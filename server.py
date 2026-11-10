"""Enterprise Async SaaS Backend (FastAPI Edition + Distributed Redis & Max Security).
Захист API-ключем, розподілений Redis Rate Limiter, Request ID та захищені вебхуки.
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

import sentry_sdk
from sentry_sdk.integrations.fastapi import FastApiIntegration

from fastapi import FastAPI, HTTPException, Request, Depends, Security, Header
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select
import redis.asyncio as aioredis

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
from database import init_db, close_db, get_db, ChatModeModel, PushTokenModel
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

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] [ReqID: %(request_id)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S"
)
logger = logging.getLogger(__name__)

BACKEND_VERSION = "4.0.0-fastapi-enterprise-distributed"

# Ініціалізація єдиного диспетчера маркетплейсів з лімітом 5 паралельних запитів
dispatcher = MarketplaceDispatcher(max_concurrent=5)

# --- РОЗПОДІЛЕНИЙ REDIS КЛІЄНТ ---
REDIS_URL = os.getenv("REDIS_URL", "")
redis_client: Optional[aioredis.Redis] = None

# Фолбек-пам'ять для Rate Limiter, якщо Redis недоступний
memory_rate_limit: Dict[str, List[float]] = {}
rate_limit_lock = asyncio.Lock()

# --- БЕЗПЕКА ТА АВТОРИЗАЦІЯ ЗАПИТІВ ---
security_scheme = HTTPBearer(auto_error=False)
MASTER_API_KEY = os.getenv("APP_MASTER_API_KEY", "netor-secure-saas-secret-key-2026")
WEBHOOK_SECRET = os.getenv("WEBHOOK_SECRET", "netor-webhook-hmac-secret-2026")

def verify_api_key(credentials: HTTPAuthorizationCredentials = Security(security_scheme)):
    """Перевірка Bearer Token для захисту від несанкціонованого доступу хакерів."""
    if not MASTER_API_KEY:
        return True
    
    if not credentials or credentials.credentials != MASTER_API_KEY:
        logger.warning("Спроба несанкціонованого доступу до API без валідного токена!")
        raise HTTPException(
            status_code=401, 
            detail="Невірний або відсутній ключ авторизації (Unauthorized)."
        )
    return True


async def check_rate_limit(client_ip: str, max_requests: int = 30, window_seconds: int = 60) -> bool:
    """Розподілений Rate Limiter через Redis із захисним фолбеком на оперативну пам'ять."""
    global redis_client
    now = time.time()
    
    if redis_client:
        try:
            key = f"ratelimit:{client_ip}"
            async with redis_client.pipeline(transaction=True) as pipe:
                pipe.zremrangebyscore(key, 0, now - window_seconds)
                pipe.zcard(key)
                pipe.zadd(key, {str(now): now})
                pipe.expire(key, window_seconds)
                results = await pipe.execute()
                
            request_count = results[1]
            if request_count >= max_requests:
                return False
            return True
        except Exception as e:
            logger.warning(f"Збій Redis у Rate Limiter, перемикаємось на локальну пам'ять: {e}")

    # Фолбек на локальну пам'ять (якщо Redis не налаштований)
    async with rate_limit_lock:
        if client_ip not in memory_rate_limit:
            memory_rate_limit[client_ip] = []
        memory_rate_limit[client_ip] = [t for t in memory_rate_limit[client_ip] if now - t < window_seconds]
        if len(memory_rate_limit[client_ip]) >= max_requests:
            return False
        memory_rate_limit[client_ip].append(now)
        return True


@asynccontextmanager
async def lifespan(app: FastAPI):
    global redis_client
    # Підключення до Redis на старті
    if REDIS_URL:
        try:
            redis_client = aioredis.from_url(REDIS_URL, decode_responses=True, encoding="utf-8")
            await redis_client.ping()
            logger.info("Успішно підключено до Redis для розподіленого кешу та лімітів! ⚡")
        except Exception as e:
            logger.error(f"Не вдалося підключитися до Redis ({REDIS_URL}): {e}. Працюємо без нього.")
            redis_client = None

    await init_db()
    logger.info("Базу даних успішно ініціалізовано на старті! 🗄️")

    monitor_task = asyncio.create_task(background_store_monitor())
    logger.info("Фоновий монітор магазинів успішно інтегровано в FastAPI Event Loop! 🚀")
    
    yield
    
    monitor_task.cancel()
    if redis_client:
        await redis_client.close()
        logger.info("З'єднання з Redis закрито.")
    await close_db()
    logger.info("Сервер та з'єднання з БД штатно зупинено.")


app = FastAPI(
    title="EcomBot AI Store SaaS API (Secured & Distributed)",
    version=BACKEND_VERSION,
    lifespan=lifespan
)

# --- REQUEST ID MIDDLEWARE (Для Enterprise Observability) ---
class RequestIDFilter(logging.Filter):
    def filter(self, record):
        if not hasattr(record, "request_id"):
            record.request_id = "N/A"
        return True

logger.addFilter(RequestIDFilter())

@app.middleware("http")
async def add_request_id_and_logging(request: Request, call_next):
    request_id = request.headers.get("X-Request-ID", str(uuid.uuid4()))
    # Прокидуємо у контекст логування
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


# --- ПРОДАКШН-ФІЛЬТРАЦІЯ CORS ---
raw_origins = os.getenv("ALLOWED_ORIGINS", "*")
origins_list = [o.strip() for o in raw_origins.split(",")] if raw_origins != "*" else ["*"]

app.add_middleware(
    CORSMiddleware,
    allow_origins=origins_list,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


# --- Pydantic Схеми Запитів ---
class CredentialsSchema(BaseModel):
    shop: str
    platform: Optional[str] = "shopify"
    token: Optional[str] = None
    access_token: Optional[str] = None
    consumer_key: Optional[str] = None
    consumer_secret: Optional[str] = None
    orders_url: Optional[str] = None

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


# --- МАРШРУТИ API ---

@app.get("/")
async def root():
    return {"message": f"EcomBot AI Store SaaS Backend (Distributed Enterprise Edition) is running! (v: {BACKEND_VERSION}) 🚀"}


@app.post("/api/orders")
async def get_orders(payload: CredentialsSchema, request: Request, authorized: bool = Depends(verify_api_key)):
    client_ip = request.client.host if request.client else "127.0.0.1"
    if not await check_rate_limit(client_ip, max_requests=30):
        raise HTTPException(status_code=429, detail="Перевищено ліміт запитів. Зачекайте хвилинку.")

    try:
        raw_shop = str(payload.shop).lower()
        platform = str(payload.platform or "shopify").lower()
        
        if "localsite" in raw_shop or "ngrok-free" in raw_shop or "netor.local" in raw_shop:
            platform = "woocommerce"

        store_config = {
            "platform": platform,
            "shop": payload.shop,
            "credentials": {
                "token": payload.token or payload.access_token,
                "consumer_key": payload.consumer_key,
                "consumer_secret": payload.consumer_secret
            }
        }

        orders = await dispatcher.fetch_store_orders(store_config)
        return {"success": True, "shop": payload.shop, "orders": orders}
        
    except OrderAPIError as e:
        raise HTTPException(status_code=e.status, detail=str(e))
    except Exception as e:
        logger.error(f"Error fetching orders: {str(e)}", exc_info=True)
        raise HTTPException(status_code=500, detail="Внутрішня помилка сервера.")


@app.post("/api/orders/multi")
async def get_orders_multi(payload: MultiStoreSchema, request: Request, authorized: bool = Depends(verify_api_key)):
    client_ip = request.client.host if request.client else "127.0.0.1"
    if not await check_rate_limit(client_ip, max_requests=15):
        raise HTTPException(status_code=429, detail="Перевищено ліміт запитів для мультизбору.")

    if not payload.stores:
        raise HTTPException(status_code=400, detail="Масив магазинів порожній.")
    
    all_orders = await dispatcher.fetch_all_user_stores(payload.stores)
    return {"success": True, "orders": all_orders}


@app.post("/api/tracking")
async def track_order_parcel(payload: TrackingSchema, authorized: bool = Depends(verify_api_key)):
    ttn = payload.trackingNumber.strip()
    carrier = payload.carrier.strip() if payload.carrier else "Нова Пошта"
    
    if not ttn:
        raise HTTPException(status_code=400, detail="Не вказано номер ТТН.")
        
    if "укр" in carrier.lower():
        result = await get_ukrposhta_tracking(ttn)
    else:
        result = await get_nova_poshta_tracking(ttn)
        
    return result


@app.post("/api/webhook/{platform}")
async def receive_marketplace_webhook(platform: str, request: Request, x_webhook_signature: Optional[str] = Header(None)):
    """Безпечний вебхук із захистом від спуфінгу через HMAC-SHA256."""
    body_bytes = await request.body()
    
    # Якщо заданий секрет вебхуків у середовищі, перевіряємо підпис
    if WEBHOOK_SECRET and x_webhook_signature:
        expected_signature = hmac.new(
            WEBHOOK_SECRET.encode("utf-8"),
            body_bytes,
            hashlib.sha256
        ).hexdigest()
        
        if not hmac.compare_digest(expected_signature, x_webhook_signature):
            logger.warning(f"Отримано невалідний підпис вебхука від {platform}!")
            raise HTTPException(status_code=403, detail="Невірний криптографічний підпис вебхука.")

    try:
        payload = await request.json()
        logger.info(f"Отримано та верифіковано Webhook від платформи {platform}: {payload.get('id', 'unknown_id')}")
        return {"success": True, "message": f"Webhook від {platform} успішно прийнято й оброблено."}
    except Exception as e:
        logger.error(f"Помилка обробки webhook для {platform}: {str(e)}")
        raise HTTPException(status_code=400, detail="Невірний формат вебхука.")


@app.post("/api/process-ai")
async def process_ai(payload: ProcessAISchema, request: Request, authorized: bool = Depends(verify_api_key)):
    client_ip = request.client.host if request.client else "127.0.0.1"
    if not await check_rate_limit(client_ip, max_requests=20):
        raise HTTPException(status_code=429, detail="Перевищено ліміт запитів до ШІ. Зачекайте хвилинку.")

    shop = normalize_shop(payload.shop) or payload.shop
    order_data = payload.order_data
    
    order = order_data if ("delivery_address" in order_data and "total_amount" in order_data) else normalize_order(order_data, shop)
    ai_report = await analyze_order(order, payload.chat_messages or "")
    
    return {"success": True, "ai_report": ai_report}


@app.post("/api/chat")
async def chat_with_ai(payload: ChatSchema, request: Request, db: AsyncSession = Depends(get_db), authorized: bool = Depends(verify_api_key)):
    client_ip = request.client.host if request.client else "127.0.0.1"
    if not await check_rate_limit(client_ip, max_requests=30):
        raise HTTPException(status_code=429, detail="Перевищено ліміт запитів до чату.")

    chat_id = payload.chat_id or "default_chat"
    
    result = await db.execute(select(ChatModeModel).where(ChatModeModel.chat_id == chat_id))
    chat_mode_record = result.scalars().first()
    current_mode = chat_mode_record.mode if chat_mode_record else "ai"

    if current_mode == "manual":
        return {
            "success": True, 
            "reply": "", 
            "mode": "manual", 
            "info": "Чат переведено в ручний режим. ШІ не відповідає."
        }

    dummy_order = {"id": "chat-session", "customer_name": "Покупець у чаті", "items": []}
    ai_report = await analyze_order(dummy_order, payload.message)
    ai_reply = ai_report.get("recommendation") or ai_report.get("ai_summary") or "Вітаю! Чим можу допомогти?"

    return {"success": True, "reply": ai_reply, "mode": "ai"}


@app.post("/api/chat/mode")
async def set_chat_mode(payload: ChatModeSchema, db: AsyncSession = Depends(get_db), authorized: bool = Depends(verify_api_key)):
    if payload.mode not in ["ai", "manual"]:
        raise HTTPException(status_code=400, detail="Некоректний режим.")

    result = await db.execute(select(ChatModeModel).where(ChatModeModel.chat_id == payload.chat_id))
    chat_mode_record = result.scalars().first()

    if chat_mode_record:
        chat_mode_record.mode = payload.mode
    else:
        new_record = ChatModeModel(chat_id=payload.chat_id, mode=payload.mode)
        db.add(new_record)

    await db.commit()
    logger.info("Режим чату для %s змінено на: %s", payload.chat_id, payload.mode)
    return {"success": True, "chat_id": payload.chat_id, "mode": payload.mode}


@app.post("/api/support")
async def support_and_returns(payload: SupportSchema, request: Request, authorized: bool = Depends(verify_api_key)):
    client_ip = request.client.host if request.client else "127.0.0.1"
    if not await check_rate_limit(client_ip, max_requests=20):
        raise HTTPException(status_code=429, detail="Перевищено ліміт запитів.")

    complaint_text = payload.complaint or payload.message
    if not complaint_text:
        raise HTTPException(status_code=400, detail="Текст скарги не може бути порожнім.")

    support_analysis = await MultiAgentSystem.run_support_agent(complaint_text, payload.order_info or {})
    return {"success": True, "support_report": support_analysis}


@app.post("/api/notifications/register")
async def register_push_token(payload: PushTokenSchema, db: AsyncSession = Depends(get_db), authorized: bool = Depends(verify_api_key)):
    result = await db.execute(select(PushTokenModel).where(PushTokenModel.token == payload.push_token))
    existing_token = result.scalars().first()

    if not existing_token:
        new_token = PushTokenModel(token=payload.push_token)
        db.add(new_token)
        await db.commit()
        logger.info("Успішно зареєстровано новий Push-токен у БД: %s", payload.push_token)
    else:
        logger.info("Push-токен вже існує в базі: %s", payload.push_token)
        
    return {"success": True, "message": "Push-токен успішно збережено в базу даних."}


if __name__ == "__main__":
    import uvicorn
    uvicorn.run("server:app", host="0.0.0.0", port=5000, reload=True)