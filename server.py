"""Enterprise Async SaaS Backend (FastAPI Edition + Distributed Redis & Max Security).
Розподілений Rate Limiter по user_id, ідемпотентність вебхуків через Redis,
ізоляція транзакцій БД (захист від вичерпання пулу з'єднань) та повна безпека.
"""

import os
import logging
import asyncio
import time
import uuid
from typing import Dict, Any, List, Optional
from contextlib import asynccontextmanager
import hmac
import hashlib
from datetime import datetime, timedelta
from pathlib import Path

# Автоматичне завантаження змінних з локального файлу .env (якщо він існує)
if Path(".env").exists():
    from dotenv import load_dotenv
    load_dotenv()

import sentry_sdk
from sentry_sdk.integrations.fastapi import FastApiIntegration

from fastapi import FastAPI, HTTPException, Request, Depends, Security, Header, status
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, EmailStr
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select
import redis.asyncio as aioredis
import jwt
import bcrypt

# Ініціалізація Sentry для продакшн-моніторингу помилок
SENTRY_DSN = os.getenv("SENTRY_DSN", "")
if SENTRY_DSN:
    sentry_sdk.init(
        dsn=SENTRY_DSN,
        integrations=[FastApiIntegration()],
        traces_sample_rate=1.0,
        environment=os.getenv("ENVIRONMENT", "production")
    )

# Внутрішні модулі системи та БД
from database import init_db, close_db, AsyncSessionLocal, ChatModeModel, PushTokenModel, UserModel
from ai_service import analyze_order
from agents import MultiAgentSystem
from order_normalizer import (
    OrderAPIError, 
    normalize_shop,
    normalize_order
)
from clients.dispatcher import MarketplaceDispatcher
from nova_poshta import get_nova_poshta_tracking
from ukrposhta import get_ukrposhta_tracking
from background_worker import background_store_monitor

# --- БЕЗПЕЧНЕ ФОРМАТУВАННЯ ЛОГІВ ---
class SafeFormatter(logging.Formatter):
    def format(self, record):
        if not hasattr(record, "request_id"):
            record.request_id = "N/A"
        return super().format(record)

handler = logging.StreamHandler()
handler.setFormatter(SafeFormatter("%(asctime)s [%(levelname)s] [ReqID: %(request_id)s] %(message)s", datefmt="%Y-%m-%d %H:%M:%S"))

root_logger = logging.getLogger()
root_logger.setLevel(logging.INFO)
root_logger.handlers = [handler]

logger = logging.getLogger(__name__)

BACKEND_VERSION = "5.0.2-fastapi-enterprise-ultimate"

dispatcher = MarketplaceDispatcher(max_concurrent=5)

# --- РОЗПОДІЛЕНИЙ REDIS КЛІЄНТ ---
REDIS_URL = os.getenv("REDIS_URL", "")
redis_client: Optional[aioredis.Redis] = None

# Фолбек-пам'ять для Rate Limiter
memory_rate_limit: Dict[str, List[float]] = {}
rate_limit_lock = asyncio.Lock()

# --- БЕЗПЕКА ТА JWT АВТОРИЗАЦІЯ ---
JWT_SECRET_KEY = os.getenv("JWT_SECRET_KEY", "netor-super-secure-jwt-secret-key-2026")
JWT_ALGORITHM = "HS256"
JWT_EXPIRATION_HOURS = 72

security_scheme = HTTPBearer(auto_error=False)
WEBHOOK_SECRET = os.getenv("WEBHOOK_SECRET", "netor-webhook-hmac-secret-2026")

def hash_password(password: str) -> str:
    return bcrypt.hashpw(password.encode('utf-8'), bcrypt.gensalt()).decode('utf-8')

def verify_password(plain_password: str, hashed_password: str) -> bool:
    return bcrypt.checkpw(plain_password.encode('utf-8'), hashed_password.encode('utf-8'))

def create_access_token(data: dict) -> str:
    to_encode = data.copy()
    expire = datetime.utcnow() + timedelta(hours=JWT_EXPIRATION_HOURS)
    to_encode.update({"exp": expire})
    return jwt.encode(to_encode, JWT_SECRET_KEY, algorithm=JWT_ALGORITHM)

async def get_current_user_id(credentials: HTTPAuthorizationCredentials = Depends(security_scheme)) -> int:
    """Витягує та верифікує user_id з JWT токена (Tenant Isolation)."""
    if not credentials:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Відсутній токен авторизації.")
    try:
        payload = jwt.decode(credentials.credentials, JWT_SECRET_KEY, algorithms=[JWT_ALGORITHM])
        user_id: int = payload.get("sub")
        if user_id is None:
            raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Невалідний JWT токен.")
        return user_id
    except jwt.ExpiredSignatureError:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Термін дії JWT токена закінчився.")
    except jwt.PyJWTError:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Помилка декодування токена.")


async def check_rate_limit(identifier: str, max_requests: int = 30, window_seconds: int = 60) -> bool:
    """Розподілений Rate Limiter через Redis за унікальним ідентифікатором (user_id або IP)."""
    global redis_client
    now = time.time()
    
    if redis_client:
        try:
            key = f"ratelimit:{identifier}"
            async with redis_client.pipeline(transaction=True) as pipe:
                pipe.zremrangebyscore(key, 0, now - window_seconds)
                pipe.zcard(key)
                pipe.zadd(key, {str(now): now})
                pipe.expire(key, window_seconds)
                results = await pipe.execute()
                
            request_count = results[1]
            return request_count < max_requests
        except Exception as e:
            logger.warning(f"Збій Redis у Rate Limiter: {e}")

    # Фолбек на локальну пам'ять
    async with rate_limit_lock:
        if identifier not in memory_rate_limit:
            memory_rate_limit[identifier] = []
        memory_rate_limit[identifier] = [t for t in memory_rate_limit[identifier] if now - t < window_seconds]
        if len(memory_rate_limit[identifier]) >= max_requests:
            return False
        memory_rate_limit[identifier].append(now)
        return True


@asynccontextmanager
async def lifespan(app: FastAPI):
    global redis_client
    if REDIS_URL:
        try:
            redis_client = aioredis.from_url(REDIS_URL, decode_responses=True, encoding="utf-8")
            await redis_client.ping()
            logger.info("Успішно підключено до Redis! ⚡")
        except Exception as e:
            logger.error(f"Не вдалося підключитися до Redis: {e}")
            redis_client = None

    await init_db()
    logger.info("Базу даних ініціалізовано! 🗄️")

    monitor_task = asyncio.create_task(background_store_monitor())
    yield
    
    monitor_task.cancel()
    if redis_client:
        await redis_client.close()
    await close_db()
    logger.info("Сервер зупинено.")


app = FastAPI(
    title="EcomBot AI Store SaaS API (Ultimate Enterprise)",
    version=BACKEND_VERSION,
    lifespan=lifespan
)

@app.middleware("http")
async def add_request_id_and_logging(request: Request, call_next):
    request_id = request.headers.get("X-Request-ID", str(uuid.uuid4()))
    
    old_factory = logging.getLogRecordFactory()
    def record_factory(*args, **kwargs):
        record = old_factory(*args, **kwargs)
        record.request_id = request_id
        return record
    logging.setLogRecordFactory(record_factory)

    start_time = time.time()
    response = await call_next(request)
    duration = (time.time() - start_time) * 1000

    response.headers["X-Request-ID"] = request_id
    logger.info(f"Path: {request.url.path} | Method: {request.method} | Status: {response.status_code} | Duration: {duration:.2f}ms")
    return response


raw_origins = os.getenv("ALLOWED_ORIGINS", "*")
origins_list = [o.strip() for o in raw_origins.split(",")] if raw_origins != "*" else ["*"]
app.add_middleware(
    CORSMiddleware,
    allow_origins=origins_list,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


# --- СХЕМИ Pydantic ---
class UserRegisterSchema(BaseModel):
    email: EmailStr
    password: str

class UserLoginSchema(BaseModel):
    email: EmailStr
    password: str

class CredentialsSchema(BaseModel):
    shop: str
    platform: Optional[str] = "shopify"
    token: Optional[str] = None
    access_token: Optional[str] = None
    consumer_key: Optional[str] = None
    consumer_secret: Optional[str] = None

class MultiStoreSchema(BaseModel):
    stores: List[Dict[str, Any]]

class TrackingSchema(BaseModel):
    trackingNumber: str
    carrier: Optional[str] = "Нова Пошта"

class ProcessAISchema(BaseModel):
    shop: str
    token: Optional[str] = None
    order_data: Dict[str, Any]
    chat_messages: Optional[str] = ""

class ChatSchema(BaseModel):
    shop: str
    token: Optional[str] = None
    message: str
    chat_id: Optional[str] = "default_chat"

class ChatModeSchema(BaseModel):
    chat_id: str
    mode: str

class SupportSchema(BaseModel):
    shop: str
    token: Optional[str] = None
    complaint: Optional[str] = None
    message: Optional[str] = None
    order_info: Optional[Dict[str, Any]] = {}

class PushTokenSchema(BaseModel):
    push_token: str


# --- АВТОРИЗАЦІЯ КОРИСТУВАЧІВ ---
@app.post("/api/auth/register")
async def register(payload: UserRegisterSchema, request: Request):
    client_ip = request.client.host if request.client else "127.0.0.1"
    if not await check_rate_limit(f"reg:{client_ip}", max_requests=5, window_seconds=60):
        raise HTTPException(status_code=429, detail="Забагато спроб реєстрації. Зачекайте.")

    async with AsyncSessionLocal() as db:
        result = await db.execute(select(UserModel).where(UserModel.email == payload.email))
        if result.scalars().first():
            raise HTTPException(status_code=400, detail="Користувач із такою електронною поштою вже існує.")
        
        new_user = UserModel(email=payload.email, hashed_password=hash_password(payload.password))
        db.add(new_user)
        await db.commit()
        await db.refresh(new_user)
        
        token = create_access_token({"sub": new_user.id, "email": new_user.email})
        return {"success": True, "token": token, "user": {"id": new_user.id, "email": new_user.email}}


@app.post("/api/auth/login")
async def login(payload: UserLoginSchema, request: Request):
    client_ip = request.client.host if request.client else "127.0.0.1"
    if not await check_rate_limit(f"login:{client_ip}", max_requests=10, window_seconds=60):
        raise HTTPException(status_code=429, detail="Забагато спроб входу. Зачекайте.")

    async with AsyncSessionLocal() as db:
        result = await db.execute(select(UserModel).where(UserModel.email == payload.email))
        user = result.scalars().first()
        
        if not user or not verify_password(payload.password, user.hashed_password):
            raise HTTPException(status_code=401, detail="Невірний email або пароль.")
        
        token = create_access_token({"sub": user.id, "email": user.email})
        return {"success": True, "token": token, "user": {"id": user.id, "email": user.email}}


# --- МАРШРУТИ API (Ізольовані та оптимізовані) ---

@app.get("/")
async def root():
    return {"message": f"EcomBot AI Store SaaS Backend (Ultimate Enterprise) is running! (v: {BACKEND_VERSION}) 🚀"}


@app.post("/api/orders")
async def get_orders(payload: CredentialsSchema, request: Request, user_id: int = Depends(get_current_user_id)):
    # Рейт-ліміт за унікальним user_id замість IP
    if not await check_rate_limit(f"user:{user_id}:orders", max_requests=30):
        raise HTTPException(status_code=429, detail="Перевищено ліміт запитів.")

    try:
        raw_shop = str(payload.shop).lower()
        platform = str(payload.platform or "shopify").lower()
        
        store_config = {
            "platform": platform,
            "shop": payload.shop,
            "credentials": {
                "token": payload.token or payload.access_token,
                "consumer_key": payload.consumer_key,
                "consumer_secret": payload.consumer_secret
            }
        }

        # Зовнішній виклик виконується поза транзакціями бази даних
        orders = await dispatcher.fetch_store_orders(store_config)
        return {"success": True, "shop": payload.shop, "orders": orders}
        
    except OrderAPIError as e:
        if e.status in [401, 403]:
            return {"success": False, "token_expired": True, "error": "Токен доступу застарів."}
        raise HTTPException(status_code=e.status, detail=str(e))
    except Exception as e:
        logger.error(f"Error fetching orders: {str(e)}", exc_info=True)
        return {"success": False, "token_expired": True, "error": "Помилка авторизації в магазині."}


@app.post("/api/orders/multi")
async def get_orders_multi(payload: MultiStoreSchema, request: Request, user_id: int = Depends(get_current_user_id)):
    if not await check_rate_limit(f"user:{user_id}:multi", max_requests=15):
        raise HTTPException(status_code=429, detail="Перевищено ліміт запитів для мультизбору.")

    if not payload.stores:
        raise HTTPException(status_code=400, detail="Масив магазинів порожній.")
    
    all_orders = await dispatcher.fetch_all_user_stores(payload.stores)
    return {"success": True, "orders": all_orders}


@app.post("/api/tracking")
async def track_order_parcel(payload: TrackingSchema, user_id: int = Depends(get_current_user_id)):
    ttn = payload.trackingNumber.strip()
    carrier = payload.carrier.strip() if payload.carrier else "Нова Пошта"
    
    if not ttn:
        raise HTTPException(status_code=400, detail="Не вказано номер ТТН.")
        
    if "укр" in carrier.lower():
        return await get_ukrposhta_tracking(ttn)
    return await get_nova_poshta_tracking(ttn)


@app.post("/api/webhook/{platform}")
async def receive_marketplace_webhook(platform: str, request: Request, x_webhook_signature: Optional[str] = Header(None)):
    """Безпечний вебхук із захистом HMAC-SHA256 та ІДЕМПОТЕНТНІСТЮ через Redis."""
    body_bytes = await request.body()
    
    if WEBHOOK_SECRET and x_webhook_signature:
        expected_signature = hmac.new(WEBHOOK_SECRET.encode("utf-8"), body_bytes, hashlib.sha256).hexdigest()
        if not hmac.compare_digest(expected_signature, x_webhook_signature):
            logger.warning(f"Невірний підпис вебхука від {platform}!")
            raise HTTPException(status_code=403, detail="Невірний криптографічний підпис.")

    # --- Ідемпотентність вебхуків ---
    event_hash = hashlib.sha256(body_bytes).hexdigest()
    idempotency_key = f"webhook:processed:{platform}:{event_hash}"
    
    if redis_client:
        try:
            # nx=True ставить ключ лише якщо його ще не було (TTL 300 секунд)
            is_new = await redis_client.set(idempotency_key, "processed", nx=True, ex=300)
            if not is_new:
                logger.info(f"Отримано дублікат вебхука від {platform} (Idempotency Triggered). Пропускаємо.")
                return {"success": True, "message": "Вебхук вже було оброблено раніше."}
        except Exception as e:
            logger.warning(f"Збій перевірки ідемпотентності в Redis: {e}")

    try:
        payload = await request.json()
        logger.info(f"Успішно прийнято новий Webhook від {platform}: {payload.get('id', 'N/A')}")
        return {"success": True, "message": f"Webhook від {platform} оброблено."}
    except Exception as e:
        logger.error(f"Помилка обробки webhook для {platform}: {str(e)}")
        raise HTTPException(status_code=400, detail="Невірний формат вебхука.")


@app.post("/api/process-ai")
async def process_ai(payload: ProcessAISchema, request: Request, user_id: int = Depends(get_current_user_id)):
    if not await check_rate_limit(f"user:{user_id}:ai", max_requests=20):
        raise HTTPException(status_code=429, detail="Перевищено ліміт запитів до ШІ.")

    shop = normalize_shop(payload.shop) or payload.shop
    order = payload.order_data if ("delivery_address" in payload.order_data) else normalize_order(payload.order_data, shop)
    
    # Виклик ШІ (Claude) виконується повністю поза транзакцією БД
    ai_report = await analyze_order(order, payload.chat_messages or "")
    
    return {"success": True, "ai_report": ai_report}


@app.post("/api/chat")
async def chat_with_ai(payload: ChatSchema, request: Request, user_id: int = Depends(get_current_user_id)):
    if not await check_rate_limit(f"user:{user_id}:chat", max_requests=30):
        raise HTTPException(status_code=429, detail="Перевищено ліміт запитів до чату.")

    chat_id = payload.chat_id or "default_chat"
    
    # Коротке з'єднання з БД виключно для читання режиму чату (ізоляція транзакції)
    async with AsyncSessionLocal() as db:
        result = await db.execute(select(ChatModeModel).where(ChatModeModel.chat_id == chat_id))
        chat_mode_record = result.scalars().first()
        current_mode = chat_mode_record.mode if chat_mode_record else "ai"

    if current_mode == "manual":
        return {"success": True, "reply": "", "mode": "manual", "info": "Чат у ручному режимі."}

    dummy_order = {"id": "chat-session", "customer_name": "Покупець у чаті", "items": []}
    
    # Зовнішній виклик ШІ поза будь-якою сесією БД
    ai_report = await analyze_order(dummy_order, payload.message)
    ai_reply = ai_report.get("recommendation") or ai_report.get("ai_summary") or "Вітаю! Чим можу допомогти?"

    return {"success": True, "reply": ai_reply, "mode": "ai"}


@app.post("/api/chat/mode")
async def set_chat_mode(payload: ChatModeSchema, user_id: int = Depends(get_current_user_id)):
    if payload.mode not in ["ai", "manual"]:
        raise HTTPException(status_code=400, detail="Некоректний режим.")

    # Коротке з'єднання для збереження режиму чату
    async with AsyncSessionLocal() as db:
        result = await db.execute(select(ChatModeModel).where(ChatModeModel.chat_id == payload.chat_id))
        chat_mode_record = result.scalars().first()

        if chat_mode_record:
            chat_mode_record.mode = payload.mode
        else:
            db.add(ChatModeModel(chat_id=payload.chat_id, mode=payload.mode))

        await db.commit()

    logger.info("Режим чату для %s змінено на: %s", payload.chat_id, payload.mode)
    return {"success": True, "chat_id": payload.chat_id, "mode": payload.mode}


@app.post("/api/support")
async def support_and_returns(payload: SupportSchema, request: Request, user_id: int = Depends(get_current_user_id)):
    if not await check_rate_limit(f"user:{user_id}:support", max_requests=20):
        raise HTTPException(status_code=429, detail="Перевищено ліміт запитів.")

    complaint_text = payload.complaint or payload.message
    if not complaint_text:
        raise HTTPException(status_code=400, detail="Текст скарги не може бути порожнім.")

    # Зовнішній виклик мультиагентної системи підтримки поза БД
    support_analysis = await MultiAgentSystem.run_support_agent(complaint_text, payload.order_info or {})
    return {"success": True, "support_report": support_analysis}


@app.post("/api/notifications/register")
async def register_push_token(payload: PushTokenSchema, user_id: int = Depends(get_current_user_id)):
    # Коротке з'єднання для реєстрації push-токена
    async with AsyncSessionLocal() as db:
        result = await db.execute(select(PushTokenModel).where(PushTokenModel.token == payload.push_token))
        if not result.scalars().first():
            db.add(PushTokenModel(token=payload.push_token))
            await db.commit()
            
    return {"success": True, "message": "Push-токен зареєстровано."}


if __name__ == "__main__":
    import uvicorn
    uvicorn.run("server:app", host="0.0.0.0", port=5000, reload=True)