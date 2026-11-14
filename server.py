import logging
from contextlib import asynccontextmanager
from typing import Optional
from fastapi import FastAPI, status
from fastapi.responses import JSONResponse
from fastapi.middleware.cors import CORSMiddleware
from redis.asyncio import Redis
from sqlalchemy import select

from config import BACKEND_VERSION
from deps import set_redis_client, get_redis
from database import AsyncSessionLocal

# Налаштування структурованого логування
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Життєвий цикл додатку: ініціалізація та закриття з'єднань з Redis."""
    redis_client: Optional[Redis] = None
    try:
        redis_client = Redis(host="localhost", port=6379, decode_responses=True)
        await redis_client.ping()
        logger.info("Успішно підключено до Redis.")
    except Exception as e:
        logger.warning(f"Не вдалося підключитися до Redis (ліміти працюватимуть у фолбек-режимі): {e}")
        redis_client = None

    set_redis_client(redis_client)
    yield

    if redis_client:
        await redis_client.close()
        logger.info("З'єднання з Redis успішно закрито.")


# Створення основного FastAPI додатку (Enterprise Core)[cite: 18]
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


# --- ЄДИНИЙ КОМПЛЕКСНИЙ HEALTH CHECK ---
@app.get("/health", status_code=status.HTTP_200_OK, tags=["System"])
async def health_check() -> JSONResponse:
    """Детальна перевірка здоров'я підсистем ШІ, бази даних та Redis."""
    checks = {"api": "ok", "database": "unknown", "redis": "unknown"}
    
    try:
        async with AsyncSessionLocal() as db:
            await db.scalar(select(1))
        checks["database"] = "ok"
    except Exception as e:
        logger.error(f"Health check DB error: {e}")
        checks["database"] = "error"
    
    redis = await get_redis()
    if redis:
        try:
            await redis.ping()
            checks["redis"] = "ok"
        except Exception as e:
            logger.error(f"Health check Redis error: {e}")
            checks["redis"] = "error"
    else:
        checks["redis"] = "not_configured"
        
    status_code = status.HTTP_200_OK if checks["database"] == "ok" else status.HTTP_503_SERVICE_UNAVAILABLE
    return JSONResponse(
        status_code=status_code, 
        content={
            "status": "healthy" if status_code == 200 else "degraded",
            "version": BACKEND_VERSION,
            "checks": checks
        }
    )


# --- ПІДКЛЮЧЕННЯ РОУТЕРІВ ---
from routers.auth import router as auth_router
from routers.stores import router as stores_router
from routers.orders import router as orders_router
from routers.ai_support import router as ai_support_router

app.include_router(auth_router)
app.include_router(stores_router)
app.include_router(orders_router)
app.include_router(ai_support_router)