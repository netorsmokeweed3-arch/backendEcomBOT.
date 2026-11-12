"""OmniFlow Enterprise SaaS Backend (FastAPI Edition + Distributed Redis & Max Security).
Розподілений Rate Limiter по user_id, ідемпотентність вебхуків через Redis,
ізоляція транзакцій БД, аудит помилок, JWT Access/Refresh токени та Revocation (Blacklist).
"""

import os
import logging
import asyncio
import time
import uuid
import json as jsonlib
from typing import Dict, Any, List, Optional
from contextlib import asynccontextmanager
from contextvars import ContextVar
import hmac
import hashlib
from datetime import datetime, timezone, timedelta
from pathlib import Path

# Автоматичне завантаження змінних з локального файлу .env (якщо він існує)
if Path(".env").exists():
    from dotenv import load_dotenv
    load_dotenv()

import sentry_sdk
from sentry_sdk.integrations.fastapi import FastApiIntegration

from fastapi import FastAPI, HTTPException, Request, Depends, Security, Header, status
from fastapi.responses import JSONResponse
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, EmailStr, field_validator
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
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
from database import (
    init_db, close_db, AsyncSessionLocal, get_db,
    ChatModeModel, PushTokenModel, UserModel, AuditLogModel, StoreCredentialModel, OrderModel,
    encrypt_credential, decrypt_credential
)
from redis_pool import get_redis, close_redis
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
from background_worker import background_store_monitor, save_orders_to_db

# --- БЕЗПЕЧНЕ ФОРМАТУВАННЯ ЛОГІВ ЧЕРЕЗ CONTEXTVARS ---
request_id_var: ContextVar[str] = ContextVar("request_id", default="N/A")

class SafeFormatter(logging.Formatter):
    def format(self, record):
        record.request_id = request_id_var.get("N/A")
        return super().format(record)

handler = logging.StreamHandler()
handler.setFormatter(SafeFormatter("%(asctime)s [%(levelname)s] [ReqID: %(request_id)s] %(message)s", datefmt="%Y-%m-%d %H:%M:%S"))

root_logger = logging.getLogger()
root_logger.setLevel(logging.INFO)
root_logger.handlers = [handler]

logger = logging.getLogger(__name__)

BACKEND_VERSION = "5.5.1-omniflow-enterprise-secure"

dispatcher = MarketplaceDispatcher(max_concurrent=5)

# Фолбек-пам'ять для Rate Limiter з обмеженням розміру (захист від витоку пам'яті)
memory_rate_limit: Dict[str, List[float]] = {}
rate_limit_lock = asyncio.Lock()

# --- БЕЗПЕКА ТА JWT АВТОРИЗАЦІЯ (Fail-Fast для JWT_SECRET_KEY) ---
JWT_SECRET_KEY = os.getenv("JWT_SECRET_KEY")
if not JWT_SECRET_KEY:
    raise RuntimeError(
        "КРИТИЧНА ПОМИЛКА БЕЗПЕКИ: Змінна середовища JWT_SECRET_KEY не задана! "
        "Сервер не може стартувати з публічно відомим ключем підпису токенів."
    )

JWT_ALGORITHM = "HS256"
JWT_ACCESS_EXPIRATION_MINUTES = 15
JWT_REFRESH_EXPIRATION_DAYS = 7

security_scheme = HTTPBearer(auto_error=False)
WEBHOOK_SECRET = os.getenv("WEBHOOK_SECRET", "")
if not WEBHOOK_SECRET:
    logger.warning("УВАГА: WEBHOOK_SECRET не задано! Перевірка підписів вебхуків відключена.")

# Неблокуючий bcrypt через потоки
def _hash_password_sync(password: str) -> str:
    return bcrypt.hashpw(password.encode('utf-8'), bcrypt.gensalt()).decode('utf-8')

def _verify_password_sync(plain_password: str, hashed_password: str) -> bool:
    return bcrypt.checkpw(plain_password.encode('utf-8'), hashed_password.encode('utf-8'))

async def hash_password(password: str) -> str:
    return await asyncio.to_thread(_hash_password_sync, password)

async def verify_password(plain_password: str, hashed_password: str) -> bool:
    return await asyncio.to_thread(_verify_password_sync, plain_password, hashed_password)

def create_access_token(data: dict) -> str:
    to_encode = data.copy()
    expire = datetime.now(timezone.utc) + timedelta(minutes=JWT_ACCESS_EXPIRATION_MINUTES)
    to_encode.update({"exp": expire, "type": "access", "jti": str(uuid.uuid4())})
    return jwt.encode(to_encode, JWT_SECRET_KEY, algorithm=JWT_ALGORITHM)

def create_refresh_token(data: dict) -> str:
    to_encode = data.copy()
    expire = datetime.now(timezone.utc) + timedelta(days=JWT_REFRESH_EXPIRATION_DAYS)
    to_encode.update({"exp": expire, "type": "refresh", "jti": str(uuid.uuid4())})
    return jwt.encode(to_encode, JWT_SECRET_KEY, algorithm=JWT_ALGORITHM)

async def log_audit_event(user_id: int, shop_domain: str, event_type: str, message: str) -> None:
    """Асинхронно записує подію або помилку в таблицю audit_logs для клієнта."""
    try:
        async with AsyncSessionLocal() as db:
            audit_entry = AuditLogModel(
                user_id=user_id,
                shop_domain=shop_domain or "unknown_shop",
                event_type=event_type,
                message=message
            )
            db.add(audit_entry)
            await db.commit()
    except Exception as e:
        logger.error(f"Не вдалося записати подію в audit_logs: {e}")


async def get_current_user_id(credentials: HTTPAuthorizationCredentials = Depends(security_scheme)) -> int:
    """Витягує, перевіряє тип Access токена та звіряє з чорним списком у Redis (Revocation)."""
    if not credentials:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Відсутній токен авторизації.")
    
    token = credentials.credentials
    try:
        payload = jwt.decode(token, JWT_SECRET_KEY, algorithms=[JWT_ALGORITHM])
        
        # Перевірка типу токена (має бути саме access)
        if payload.get("type") != "access":
            raise HTTPException(status_code=401, detail="Невалідний тип токена. Очікується access-токен.")
            
        sub = payload.get("sub")
        jti = payload.get("jti")
        if sub is None or jti is None:
            raise HTTPException(status_code=401, detail="Невалідний payload JWT токена.")
            
        # Перевірка чорного списку (Revocation) через Redis
        redis = await get_redis()
        if redis:
            is_revoked = await redis.get(f"token:revoked:{jti}")
            if is_revoked:
                raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Токен було відкликано (Revoked). Увійдіть знову.")
                
        return int(sub)
        
    except jwt.ExpiredSignatureError:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Термін дії JWT токена закінчився.")
    except (jwt.PyJWTError, ValueError, TypeError):
        raise HTTPException(status_code=401, detail="Помилка декодування або невалідний формат токена.")


async def check_rate_limit(identifier: str, max_requests: int = 30, window_seconds: int = 60) -> bool:
    """Розподілений Rate Limiter через Redis за унікальним ідентифікатором (user_id або IP)."""
    redis = await get_redis()
    now = time.time()
    
    if redis:
        try:
            key = f"ratelimit:{identifier}"
            async with redis.pipeline(transaction=True) as pipe:
                pipe.zremrangebyscore(key, 0, now - window_seconds)
                pipe.zcard(key)
                pipe.zadd(key, {str(uuid.uuid4()): now})
                pipe.expire(key, window_seconds)
                results = await pipe.execute()
                
            request_count = results[1]
            return request_count < max_requests
        except Exception as e:
            logger.warning(f"Збій Redis у Rate Limiter: {e}")

    async with rate_limit_lock:
        if len(memory_rate_limit) > 10_000:
            for k in [k for k, v in memory_rate_limit.items() if not v or now - v[-1] > window_seconds]:
                memory_rate_limit.pop(k, None)
        if identifier not in memory_rate_limit:
            memory_rate_limit[identifier] = []
        memory_rate_limit[identifier] = [t for t in memory_rate_limit[identifier] if now - t < window_seconds]
        if len(memory_rate_limit[identifier]) >= max_requests:
            return False
        memory_rate_limit[identifier].append(now)
        return True


@asynccontextmanager
async def lifespan(app: FastAPI):
    redis = await get_redis()
    if redis:
        try:
            await redis.ping()
            logger.info("OmniFlow: Успішно підключено до Redis! ⚡")
        except Exception as e:
            logger.error(f"Не вдалося підключитися до Redis: {e}")

    await init_db()
    logger.info("OmniFlow: Базу даних ініціалізовано! 🗄️")

    # --- ЗАПУСК ФОНОВОГО ВОРКЕРА МОНІТОРИНГУ ТА ЧЕРГИ ---
    bg_worker_task = asyncio.create_task(background_store_monitor())
    logger.info("OmniFlow: Background store monitor & queue worker запущено у фоні! 🚀")

    yield
    
    # --- КОРЕКТНЕ ЗАВЕРШЕННЯ ФОНОВОГО ВОРКЕРА ---
    bg_worker_task.cancel()
    try:
        await bg_worker_task
    except asyncio.CancelledError:
        pass

    await close_redis()
    await close_db()
    logger.info("OmniFlow: Сервер зупинено.")


app = FastAPI(
    title="OmniFlow Enterprise SaaS API",
    version=BACKEND_VERSION,
    lifespan=lifespan
)

@app.middleware("http")
async def add_request_id_and_logging(request: Request, call_next):
    request_id = request.headers.get("X-Request-ID", str(uuid.uuid4()))
    token = request_id_var.set(request_id)

    start_time = time.time()
    try:
        response = await call_next(request)
    finally:
        request_id_var.reset(token)

    duration = (time.time() - start_time) * 1000
    response.headers["X-Request-ID"] = request_id
    logger.info(f"Path: {request.url.path} | Method: {request.method} | Status: {response.status_code} | Duration: {duration:.2f}ms")
    return response


raw_origins = os.getenv("ALLOWED_ORIGINS", "")
if not raw_origins:
    logger.warning("ALLOWED_ORIGINS не задано — CORS працює в dev-режимі (localhost).")
    origins_list = ["http://localhost:3000", "http://localhost:8081", "http://localhost:19006"]
else:
    origins_list = [o.strip() for o in raw_origins.split(",") if o.strip()]

app.add_middleware(
    CORSMiddleware,
    allow_origins=origins_list,
    allow_credentials=True,
    allow_methods=["GET", "POST", "PUT", "DELETE"],
    allow_headers=["Authorization", "Content-Type", "X-Request-ID", "X-Webhook-Signature"],
)


# --- СХЕМИ Pydantic ---
class UserRegisterSchema(BaseModel):
    email: EmailStr
    password: str

    @field_validator("password")
    @classmethod
    def validate_password(cls, v: str) -> str:
        if len(v) < 8:
            raise ValueError("Пароль має містити щонайменше 8 символів.")
        # Суворе обмеження довжини до 72 байтів через специфіку bcrypt
        if len(v.encode('utf-8')) > 72:
            raise ValueError("Пароль занадто довгий (максимум 72 байти для bcrypt).")
        return v

class UserLoginSchema(BaseModel):
    email: EmailStr
    password: str

class RefreshTokenSchema(BaseModel):
    refresh_token: str

class LogoutSchema(BaseModel):
    refresh_token: Optional[str] = None

class StoreCreateSchema(BaseModel):
    shop: str
    platform: str = "prom"
    token: Optional[str] = None
    access_token: Optional[str] = None
    consumer_key: Optional[str] = None
    consumer_secret: Optional[str] = None

class CredentialsSchema(BaseModel):
    shop: str
    platform: Optional[str] = None
    token: Optional[str] = None
    access_token: Optional[str] = None
    consumer_key: Optional[str] = None
    consumer_secret: Optional[str] = None

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


# --- СХЕМИ ВІДПОВІДЕЙ (Swagger / OpenAPI) ---
class AuthUserOut(BaseModel):
    id: int
    email: str

class AuthResponse(BaseModel):
    success: bool
    access_token: str
    refresh_token: str
    user: AuthUserOut

class TokenRefreshResponse(BaseModel):
    success: bool
    access_token: str
    refresh_token: str

class OrdersResponse(BaseModel):
    success: bool
    shop: Optional[str] = None
    orders: List[Dict[str, Any]] = []
    token_expired: Optional[bool] = None
    error: Optional[str] = None

class ChatResponse(BaseModel):
    success: bool
    reply: str
    mode: str
    info: Optional[str] = None

class StandardStatusResponse(BaseModel):
    success: bool
    message: Optional[str] = None


# --- АВТОРИЗАЦІЯ ТА СЕСІЇ (AUTH & TOKENS) ---
@app.post("/api/auth/register", response_model=AuthResponse, tags=["Auth"])
async def register(payload: UserRegisterSchema, request: Request, db: AsyncSession = Depends(get_db)):
    client_ip = request.client.host if request.client else "127.0.0.1"
    if not await check_rate_limit(f"reg:{client_ip}", max_requests=5, window_seconds=60):
        raise HTTPException(status_code=429, detail="Забагато спроб реєстрації. Зачекайте.")

    result = await db.execute(select(UserModel).where(UserModel.email == payload.email))
    if result.scalars().first():
        raise HTTPException(status_code=400, detail="Користувач із такою електронною поштою вже існує.")
    
    hashed_pwd = await hash_password(payload.password)
    new_user = UserModel(email=payload.email, hashed_password=hashed_pwd)
    db.add(new_user)
    try:
        await db.commit()
    except IntegrityError:
        await db.rollback()
        raise HTTPException(status_code=400, detail="Користувач із такою електронною поштою вже існує.")
    except Exception:
        await db.rollback()
        raise HTTPException(status_code=400, detail="Помилка реєстрації користувача.")
    await db.refresh(new_user)
    
    access_token = create_access_token({"sub": str(new_user.id), "email": new_user.email})
    refresh_token = create_refresh_token({"sub": str(new_user.id)})
    
    return {
        "success": True, 
        "access_token": access_token, 
        "refresh_token": refresh_token,
        "user": {"id": new_user.id, "email": new_user.email}
    }


@app.post("/api/auth/login", response_model=AuthResponse, tags=["Auth"])
async def login(payload: UserLoginSchema, request: Request, db: AsyncSession = Depends(get_db)):
    client_ip = request.client.host if request.client else "127.0.0.1"
    if not await check_rate_limit(f"login:{client_ip}", max_requests=10, window_seconds=60):
        raise HTTPException(status_code=429, detail="Забагато спроб входу. Зачекайте.")

    result = await db.execute(select(UserModel).where(UserModel.email == payload.email))
    user = result.scalars().first()
    
    if not user or not await verify_password(payload.password, user.hashed_password):
        raise HTTPException(status_code=401, detail="Невірний email або пароль.")
    
    access_token = create_access_token({"sub": str(user.id), "email": user.email})
    refresh_token = create_refresh_token({"sub": str(user.id)})
    
    return {
        "success": True, 
        "access_token": access_token, 
        "refresh_token": refresh_token,
        "user": {"id": user.id, "email": user.email}
    }


@app.post("/api/auth/refresh", response_model=TokenRefreshResponse, tags=["Auth"])
async def refresh_token(payload: RefreshTokenSchema):
    """Видає нову пару access та refresh токенів з ротацією та захистом від повторного використання (Reuse Detection)."""
    try:
        decoded = jwt.decode(payload.refresh_token, JWT_SECRET_KEY, algorithms=[JWT_ALGORITHM])
        if decoded.get("type") != "refresh":
            raise HTTPException(status_code=401, detail="Невалідний тип токена. Очікується refresh-токен.")
            
        user_id = decoded.get("sub")
        jti = decoded.get("jti")
        exp = decoded.get("exp")
        
        if not user_id or not jti:
            raise HTTPException(status_code=401, detail="Невалідний payload refresh-токена.")

        redis = await get_redis()
        if redis:
            # Перевірка на повторне використання вже скомпрометованого refresh-токена (Reuse Detection)
            is_revoked = await redis.get(f"token:revoked:{jti}")
            if is_revoked:
                logger.warning(f"УВАГА: Виявлено спробу повторного використання старого refresh-токена для user_id={user_id}!")
                raise HTTPException(status_code=401, detail="Спроба повторного використання токена. Будь ласка, увійдіть знову.")

            # РОТАЦІЯ: Одразу блеклистимо використаний старий refresh-токен на час, що залишився
            ttl = int(exp - time.time())
            if ttl > 0:
                await redis.set(f"token:revoked:{jti}", "reused", ex=ttl)

        # Генеруємо новий access-токен та ОБОВ'ЯЗКОВО новий refresh-токен (повна ротація)
        new_access_token = create_access_token({"sub": str(user_id)})
        new_refresh_token = create_refresh_token({"sub": str(user_id)})
        
        return {
            "success": True, 
            "access_token": new_access_token,
            "refresh_token": new_refresh_token
        }
        
    except jwt.ExpiredSignatureError:
        raise HTTPException(status_code=401, detail="Термін дії refresh-токена закінчився. Увійдіть знову.")
    except (jwt.PyJWTError, ValueError, TypeError):
        raise HTTPException(status_code=401, detail="Помилка валідації refresh-токена.")


@app.post("/api/auth/logout", response_model=StandardStatusResponse, tags=["Auth"])
async def logout(payload: Optional[LogoutSchema] = None, credentials: HTTPAuthorizationCredentials = Depends(security_scheme)):
    """Відкликає поточний access-токен та опціональний refresh-токен (Revocation / Blacklist у Redis)."""
    redis = await get_redis()
    
    # 1. Відкликаємо access-токен із заголовка Authorization
    if credentials:
        try:
            token_payload = jwt.decode(credentials.credentials, JWT_SECRET_KEY, algorithms=[JWT_ALGORITHM], options={"verify_signature": True})
            jti = token_payload.get("jti")
            exp = token_payload.get("exp")
            
            if jti and exp and redis:
                ttl = int(exp - time.time())
                if ttl > 0:
                    await redis.set(f"token:revoked:{jti}", "revoked", ex=ttl)
        except Exception as e:
            logger.warning(f"Помилка декодування access-токена під час логауту: {e}")

    # 2. Відкликаємо refresh-токен із тіла запиту, якщо передано
    if payload and payload.refresh_token:
        try:
            ref_payload = jwt.decode(payload.refresh_token, JWT_SECRET_KEY, algorithms=[JWT_ALGORITHM], options={"verify_signature": True})
            ref_jti = ref_payload.get("jti")
            ref_exp = ref_payload.get("exp")
            
            if ref_jti and ref_exp and redis:
                ref_ttl = int(ref_exp - time.time())
                if ref_ttl > 0:
                    await redis.set(f"token:revoked:{ref_jti}", "revoked", ex=ref_ttl)
        except Exception as e:
            logger.warning(f"Помилка декодування refresh-токена під час логауту: {e}")

    return {"success": True, "message": "Успішний вихід із системи. Сесію завершено, токени відкликано."}


# --- МАРШРУТИ МАГАЗИНІВ (СТОРІНКА /API/STORES) ---
@app.post("/api/stores", tags=["Stores"])
async def add_store(payload: StoreCreateSchema, user_id: int = Depends(get_current_user_id), db: AsyncSession = Depends(get_db)):
    """Зберігає креденшали магазину в зашифрованому вигляді в БД."""
    shop = normalize_shop(payload.shop)
    if not shop:
        raise HTTPException(status_code=400, detail="Некоректний домен магазину.")
    
    credentials = {
        "token": payload.token or payload.access_token or "",
        "consumer_key": payload.consumer_key or "",
        "consumer_secret": payload.consumer_secret or "",
    }
    if not any(credentials.values()):
        raise HTTPException(status_code=400, detail="Не передано жодного токена або ключа доступу.")
    
    encrypted = encrypt_credential(jsonlib.dumps(credentials))
    
    result = await db.execute(select(StoreCredentialModel).where(StoreCredentialModel.shop_domain == shop))
    existing = result.scalars().first()
    if existing:
        if existing.user_id != user_id:
            raise HTTPException(status_code=403, detail="Цей магазин вже підключено іншим акаунтом.")
        existing.platform = payload.platform
        existing.encrypted_credentials = encrypted
    else:
        db.add(StoreCredentialModel(
            user_id=user_id,
            shop_domain=shop,
            platform=payload.platform,
            encrypted_credentials=encrypted,
        ))
    await db.commit()
    return {"success": True, "shop": shop, "platform": payload.platform}


@app.get("/api/stores", tags=["Stores"])
async def list_stores(user_id: int = Depends(get_current_user_id), db: AsyncSession = Depends(get_db)):
    """Повертає список підключених магазинів поточного користувача (без секретів)."""
    result = await db.execute(select(StoreCredentialModel).where(StoreCredentialModel.user_id == user_id))
    stores = result.scalars().all()
    return {
        "success": True,
        "stores": [{"id": s.id, "shop": s.shop_domain, "platform": s.platform} for s in stores]
    }


@app.delete("/api/stores/{store_id}", tags=["Stores"])
async def delete_store(store_id: int, user_id: int = Depends(get_current_user_id), db: AsyncSession = Depends(get_db)):
    """Видаляє підключений магазин із перевіркою тенанта."""
    result = await db.execute(select(StoreCredentialModel).where(
        StoreCredentialModel.id == store_id,
        StoreCredentialModel.user_id == user_id,
    ))
    store = result.scalars().first()
    if not store:
        raise HTTPException(status_code=404, detail="Магазин не знайдено.")
    await db.delete(store)
    await db.commit()
    return {"success": True}


# --- ДОПОМІЖНА ФУНКЦІЯ ЗАВАНТАЖЕННЯ КРЕДЕНШАЛІВ ---
async def _load_user_store_configs(user_id: int, db: AsyncSession, shop: Optional[str] = None) -> List[Dict[str, Any]]:
    query = select(StoreCredentialModel).where(StoreCredentialModel.user_id == user_id)
    if shop:
        query = query.where(StoreCredentialModel.shop_domain == shop)
    result = await db.execute(query)
    stores = result.scalars().all()
    configs = []
    for s in stores:
        try:
            credentials = jsonlib.loads(decrypt_credential(s.encrypted_credentials))
        except Exception:
            logger.critical(f"Неможливо розшифрувати креденшали магазину {s.shop_domain}")
            continue
        configs.append({"platform": s.platform, "shop": s.shop_domain, "credentials": credentials})
    return configs


# --- МАРШРУТИ API ---

@app.get("/", tags=["System"])
async def root():
    return {"message": f"OmniFlow Enterprise SaaS Backend is running! (v: {BACKEND_VERSION}) 🚀"}


@app.get("/health", tags=["System"])
async def health_check():
    """Ендпоінт перевірки здоров'я сервісу для Docker / Cloud-провайдерів."""
    checks = {"api": "ok", "database": "unknown", "redis": "unknown"}
    try:
        async with AsyncSessionLocal() as db:
            await db.execute(select(1))
        checks["database"] = "ok"
    except Exception:
        checks["database"] = "error"
    
    redis = await get_redis()
    if redis:
        try:
            await redis.ping()
            checks["redis"] = "ok"
        except Exception:
            checks["redis"] = "error"
    else:
        checks["redis"] = "not_configured"
        
    status_code = 200 if checks["database"] == "ok" else 503
    return JSONResponse(
        status_code=status_code, 
        content={
            "status": "healthy" if status_code == 200 else "degraded",
            "version": BACKEND_VERSION,
            "checks": checks
        }
    )


@app.post("/api/orders", response_model=OrdersResponse, tags=["Orders"])
async def get_orders(payload: CredentialsSchema, user_id: int = Depends(get_current_user_id), db: AsyncSession = Depends(get_db)):
    """Отримує замовлення з персистентної бази даних (швидкий пошук без запитів до маркетплейсу в реальному часі)."""
    if not await check_rate_limit(f"user:{user_id}:orders", max_requests=30):
        raise HTTPException(status_code=429, detail="Перевищено ліміт запитів.")

    shop = normalize_shop(payload.shop) or payload.shop
    
    result = await db.execute(
        select(StoreCredentialModel).where(
            StoreCredentialModel.user_id == user_id,
            StoreCredentialModel.shop_domain == shop
        )
    )
    store = result.scalars().first()
    if not store:
        raise HTTPException(status_code=404, detail="Магазин не знайдено або він не належить вашому акаунту.")

    orders_result = await db.execute(
        select(OrderModel).where(OrderModel.shop_domain == shop)
    )
    db_orders = orders_result.scalars().all()

    orders_list = []
    for o in db_orders:
        if o.raw_payload:
            try:
                orders_list.append(jsonlib.loads(o.raw_payload))
                continue
            except Exception:
                pass
        orders_list.append({
            "id": o.external_order_id,
            "order_number": o.order_number,
            "customer_name": o.customer_name,
            "phone": o.phone,
            "email": o.email,
            "total_amount": o.total_amount,
            "currency": o.currency,
            "status": o.status,
            "created_at": o.created_at.isoformat() if o.created_at else None
        })

    return {"success": True, "shop": shop, "orders": orders_list}


@app.post("/api/orders/multi", tags=["Orders"])
async def get_orders_multi(user_id: int = Depends(get_current_user_id), db: AsyncSession = Depends(get_db)):
    """Безпечний збір замовлень з УСІХ підключених магазинів користувача з бази даних."""
    if not await check_rate_limit(f"user:{user_id}:multi", max_requests=15):
        raise HTTPException(status_code=429, detail="Перевищено ліміт запитів для мультизбору.")

    result = await db.execute(select(StoreCredentialModel).where(StoreCredentialModel.user_id == user_id))
    stores = result.scalars().all()
    if not stores:
        raise HTTPException(status_code=404, detail="У вас немає підключених магазинів.")

    shop_domains = [s.shop_domain for s in stores]
    orders_result = await db.execute(
        select(OrderModel).where(OrderModel.shop_domain.in_(shop_domains))
    )
    db_orders = orders_result.scalars().all()

    all_orders = []
    for o in db_orders:
        if o.raw_payload:
            try:
                all_orders.append(jsonlib.loads(o.raw_payload))
                continue
            except Exception:
                pass
        all_orders.append({
            "id": o.external_order_id,
            "shop": o.shop_domain,
            "order_number": o.order_number,
            "customer_name": o.customer_name,
            "total_amount": o.total_amount,
            "status": o.status
        })

    return {"success": True, "orders": all_orders}


@app.post("/api/tracking", tags=["Logistics"])
async def track_order_parcel(payload: TrackingSchema, user_id: int = Depends(get_current_user_id)):
    # Rate Limit на відстеження посилок для захисту від зловживань та спаму
    if not await check_rate_limit(f"user:{user_id}:tracking", max_requests=15, window_seconds=60):
        raise HTTPException(status_code=429, detail="Перевищено ліміт запитів на відстеження посилок. Зачекайте.")

    ttn = payload.trackingNumber.strip()
    carrier = payload.carrier.strip() if payload.carrier else "Нова Пошта"
    
    if not ttn:
        raise HTTPException(status_code=400, detail="Не вказано номер ТТН.")
        
    if "укр" in carrier.lower():
        return await get_ukrposhta_tracking(ttn)
    return await get_nova_poshta_tracking(ttn)


@app.post("/api/webhook/{platform}", tags=["Webhooks"])
async def receive_marketplace_webhook(platform: str, request: Request, x_webhook_signature: Optional[str] = Header(None), db: AsyncSession = Depends(get_db)):
    body_bytes = await request.body()
    
    if WEBHOOK_SECRET:
        if not x_webhook_signature:
            logger.warning(f"Вебхук від {platform} відхилено: відсутній підпис X-Webhook-Signature.")
            raise HTTPException(status_code=403, detail="Відсутній криптографічний підпис вебхука.")
        expected_signature = hmac.new(WEBHOOK_SECRET.encode("utf-8"), body_bytes, hashlib.sha256).hexdigest()
        if not hmac.compare_digest(expected_signature, x_webhook_signature):
            logger.warning(f"Невірний підпис вебхука від {platform}!")
            raise HTTPException(status_code=403, detail="Невірний криптографічний підпис.")

    event_hash = hashlib.sha256(body_bytes).hexdigest()
    idempotency_key = f"webhook:processed:{platform}:{event_hash}"
    
    redis = await get_redis()
    if redis:
        try:
            is_new = await redis.set(idempotency_key, "processed", nx=True, ex=300)
            if not is_new:
                logger.info(f"Отримано дублікат вебхука від {platform} (Idempotency Triggered). Пропускаємо.")
                return {"success": True, "message": "Вебхук вже було оброблено раніше."}
        except Exception as e:
            logger.warning(f"Збій перевірки ідемпотентності в Redis: {e}")

    try:
        payload = await request.json()
        logger.info(f"OmniFlow: Успішно прийнято новий Webhook від {platform}: {payload.get('id', 'N/A')}")
        
        # Справжня обробка та збереження даних вебхука в БД (або чергу)
        shop_domain = payload.get("shop") or payload.get("domain") or f"webhook-{platform}.local"
        normalized_shop_domain = normalize_shop(shop_domain) or shop_domain
        
        # Шукаємо власника цього магазину в базі
        store_result = await db.execute(select(StoreCredentialModel).where(StoreCredentialModel.shop_domain == normalized_shop_domain))
        store_record = store_result.scalars().first()
        
        if store_record:
            user_id = store_record.user_id
            # Якщо вебхук містить дані замовлення — зберігаємо їх
            order_data = payload.get("order") or payload
            if isinstance(order_data, dict) and ("id" in order_data or "order_number" in order_data):
                await save_orders_to_db(db, user_id, normalized_shop_domain, [order_data])
            
            # Логуємо успішну подію вебхука в audit_logs
            db.add(AuditLogModel(
                user_id=user_id,
                shop_domain=normalized_shop_domain,
                event_type=f"WEBHOOK_{platform.upper()}_RECEIVED",
                message=f"Успішно опрацьовано вебхук подію для магазину {normalized_shop_domain}"
            ))
            await db.commit()
            
        return {"success": True, "message": f"Webhook від {platform} успішно опрацьовано та збережено."}
    except Exception as e:
        logger.error(f"Помилка обробки webhook для {platform}: {str(e)}", exc_info=True)
        raise HTTPException(status_code=400, detail="Невірний формат вебхука або помилка обробки.")


@app.post("/api/process-ai", tags=["AI & Chat"])
async def process_ai(payload: ProcessAISchema, request: Request, user_id: int = Depends(get_current_user_id)):
    if not await check_rate_limit(f"user:{user_id}:ai", max_requests=20):
        raise HTTPException(status_code=429, detail="Перевищено ліміт запитів до ШІ.")

    shop = normalize_shop(payload.shop) or payload.shop
    order = payload.order_data if ("delivery_address" in payload.order_data) else normalize_order(payload.order_data, shop)
    
    ai_report = await analyze_order(order, payload.chat_messages or "", user_id=user_id, shop_domain=shop)
    return {"success": True, "ai_report": ai_report}


@app.post("/api/chat", response_model=ChatResponse, tags=["AI & Chat"])
async def chat_with_ai(payload: ChatSchema, request: Request, user_id: int = Depends(get_current_user_id), db: AsyncSession = Depends(get_db)):
    if not await check_rate_limit(f"user:{user_id}:chat", max_requests=30):
        raise HTTPException(status_code=429, detail="Перевищено ліміт запитів до чату.")

    chat_id = payload.chat_id or "default_chat"
    shop = normalize_shop(payload.shop) or payload.shop
    
    result = await db.execute(select(ChatModeModel).where(
        ChatModeModel.chat_id == chat_id,
        ChatModeModel.user_id == user_id
    ))
    chat_mode_record = result.scalars().first()
    current_mode = chat_mode_record.mode if chat_mode_record else "ai"

    if current_mode == "manual":
        return {"success": True, "reply": "", "mode": "manual", "info": "Чат у ручному режимі."}

    dummy_order = {"id": "chat-session", "customer_name": "Покупець у чаті", "items": []}
    ai_report = await analyze_order(dummy_order, payload.message, user_id=user_id, shop_domain=shop)
    ai_reply = ai_report.get("recommendation") or ai_report.get("ai_summary") or "Вітаю! Чим можу допомогти?"

    return {"success": True, "reply": ai_reply, "mode": "ai"}


@app.post("/api/chat/mode", tags=["AI & Chat"])
async def set_chat_mode(payload: ChatModeSchema, user_id: int = Depends(get_current_user_id), db: AsyncSession = Depends(get_db)):
    if payload.mode not in ["ai", "manual"]:
        raise HTTPException(status_code=400, detail="Некоректний режим.")

    result = await db.execute(select(ChatModeModel).where(
        ChatModeModel.chat_id == payload.chat_id,
        ChatModeModel.user_id == user_id
    ))
    chat_mode_record = result.scalars().first()

    if chat_mode_record:
        chat_mode_record.mode = payload.mode
    else:
        db.add(ChatModeModel(chat_id=payload.chat_id, user_id=user_id, mode=payload.mode))

    await db.commit()

    return {"success": True, "chat_id": payload.chat_id, "mode": payload.mode}


@app.post("/api/support", tags=["Support"])
async def support_and_returns(payload: SupportSchema, request: Request, user_id: int = Depends(get_current_user_id)):
    if not await check_rate_limit(f"user:{user_id}:support", max_requests=20):
        raise HTTPException(status_code=429, detail="Перевищено ліміт запитів.")

    complaint_text = payload.complaint or payload.message
    if not complaint_text:
        raise HTTPException(status_code=400, detail="Текст скарги не може бути порожнім.")

    support_analysis = await MultiAgentSystem.run_support_agent(complaint_text, payload.order_info or {})
    return {"success": True, "support_report": support_analysis}


@app.post("/api/notifications/register", response_model=StandardStatusResponse, tags=["Notifications"])
async def register_push_token(payload: PushTokenSchema, user_id: int = Depends(get_current_user_id), db: AsyncSession = Depends(get_db)):
    result = await db.execute(select(PushTokenModel).where(PushTokenModel.token == payload.push_token))
    existing = result.scalars().first()
    if existing:
        existing.user_id = user_id
    else:
        db.add(PushTokenModel(user_id=user_id, token=payload.push_token))
    await db.commit()
            
    return {"success": True, "message": "Push-токен зареєстровано."}


if __name__ == "__main__":
    import uvicorn
    uvicorn.run("server:app", host="0.0.0.0", port=5000, reload=True)