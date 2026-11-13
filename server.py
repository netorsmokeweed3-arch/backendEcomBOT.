import logging
from contextlib import asynccontextmanager
from typing import Optional
from fastapi import FastAPI, Request, HTTPException, Depends, status
from fastapi.responses import JSONResponse
from fastapi.middleware.cors import CORSMiddleware
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from redis.asyncio import Redis
import jwt

# Налаштування структурованого логування
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)

# Конфігурація безпеки та токенів
JWT_SECRET_KEY = "your-super-secret-jwt-key-change-in-production"
JWT_ALGORITHM = "HS256"
JWT_ACCESS_EXPIRATION_MINUTES = 15
JWT_REFRESH_EXPIRATION_DAYS = 7
BACKEND_VERSION = "2.0.0"
WEBHOOK_SECRET = "your-webhook-secret-change-in-production"

redis_client: Optional[Redis] = None
security_scheme = HTTPBearer(auto_error=False)


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Життєвий цикл додатку: ініціалізація та закриття з'єднань з Redis."""
    global redis_client
    try:
        redis_client = Redis(host="localhost", port=6379, decode_responses=True)
        await redis_client.ping()
        logger.info("Успішно підключено до Redis.")
    except Exception as e:
        logger.warning(f"Не вдалося підключитися до Redis (ліміти працюватимуть у фолбек-режимі): {e}")
        redis_client = None

    yield

    if redis_client:
        await redis_client.close()
        logger.info("З'єднання з Redis успішно закрито.")


# Створення основного FastAPI додатку (Enterprise Core)
app = FastAPI(
    title="Enterprise Marketplace Core API",
    version=BACKEND_VERSION,
    lifespan=lifespan
)

# Налаштування CORS
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


# --- ГЛОБАЛЬНІ СЕРВЕРНІ ЗАЛЕЖНОСТІ ТА УТИЛІТИ ---
async def get_redis() -> Optional[Redis]:
    return redis_client


async def check_rate_limit(identifier: str, max_requests: int, window_seconds: int = 60) -> bool:
    """Глобальна перевірка лімітів через Redis із безпечним фолбеком при збоях."""
    if not redis_client:
        return True
    try:
        current = await redis_client.incr(identifier)
        if current == 1:
            await redis_client.expire(identifier, window_seconds)
        return current <= max_requests
    except Exception as e:
        logger.error(f"Помилка Rate Limiter у Redis: {e}")
        return True


async def get_current_user_id(credentials: Optional[HTTPAuthorizationCredentials] = Depends(security_scheme)) -> int:
    """Витягує та валідує user_id з валідного Access Token з перевіркою чорного списку (Revocation)."""
    if not credentials:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Відсутній токен авторизації.")
    try:
        payload = jwt.decode(credentials.credentials, JWT_SECRET_KEY, algorithms=[JWT_ALGORITHM])
        if payload.get("type") != "access":
            raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Невалідний тип токена.")
        
        user_id = payload.get("sub")
        jti = payload.get("jti")
        
        if not user_id:
            raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Невірний payload токена.")
        
        if redis_client and jti and await redis_client.get(f"token:revoked:{jti}"):
            raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Токен відкликано.")
            
        return int(user_id)
        
    except jwt.ExpiredSignatureError:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Термін дії токена закінчився.")
    except (jwt.PyJWTError, ValueError):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Помилка валідації токена.")


# --- ПІДКЛЮЧЕННЯ РОУТЕРІВ (ПЕРЕНЕСЕНО В КІНЕЦЬ ДЛЯ УНИКНЕННЯ ЦИКЛІЧНОГО ІМПОРТУ) ---
from routers.auth import router as auth_router
from routers.stores import router as stores_router
from routers.orders import router as orders_router
from routers.ai_support import router as ai_support_router

app.include_router(auth_router)
app.include_router(stores_router)
app.include_router(orders_router)
app.include_router(ai_support_router)


@app.get("/health", tags=["System"])
async def health_check():
    """Перевірка стану мікросервісу."""
    return {"status": "healthy", "service": "active", "version": BACKEND_VERSION}