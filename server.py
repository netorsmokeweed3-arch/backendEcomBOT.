import os
import logging
from dotenv import load_dotenv

# Завантажуємо змінні середовища з файлу .env
load_dotenv()

# --- Експорт секрету верифікації вебхуків для тестів та роутерів ---
WEBHOOK_SECRET = os.getenv("WEBHOOK_SECRET", "test-webhook-secret-key-12345")

from contextlib import asynccontextmanager
from fastapi import FastAPI, status, Request
from fastapi.responses import JSONResponse
from fastapi.middleware.cors import CORSMiddleware
from sqlalchemy import select
import httpx

# --- ІНТЕГРАЦІЯ SENTRY ДЛЯ МОНІТОРИНГУ ПОМИЛОК ---
import sentry_sdk
from sentry_sdk.integrations.fastapi import FastApiIntegration

sentry_dsn = os.getenv("SENTRY_DSN")
if sentry_dsn:
    sentry_sdk.init(
        dsn=sentry_dsn,
        integrations=[FastApiIntegration()],
        traces_sample_rate=1.0,
        environment=os.getenv("ENVIRONMENT", "production"),
    )

from config import BACKEND_VERSION
from redis_pool import get_redis, close_redis
from database import AsyncSessionLocal

# Налаштування структурованого логування
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)

# Отримуємо дані для Telegram-сповіщень
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")


async def send_telegram_alert(error_message: str):
    """Асинхронно надсилає сповіщення про критичну помилку в Telegram."""
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        return
    
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {
        "chat_id": TELEGRAM_CHAT_ID,
        "text": f"🚨 **Критична помилка на сервері OmniFlow!**\n\n`{error_message}`",
        "parse_mode": "Markdown"
    }
    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            await client.post(url, json=payload)
    except Exception as e:
        logger.error(f"Не вдалося надіслати сповіщення в Telegram: {e}")


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Життєвий цикл додатку: ініціалізація та закриття пулу Redis."""
    redis = await get_redis()
    if redis:
        logger.info("Успішно підключено до Redis через єдиний пул з'єднань.")
    else:
        logger.warning("Не вдалося підключитися до Redis (ліміти працюватимуть у фолбек-режимі).")
    
    yield

    await close_redis()
    logger.info("З'єднання з Redis успішно закрито при зупинці сервера.")


# Створення основного FastAPI додатку
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


# --- ГЛОБАЛЬНИЙ ОБРОБНИК КРИТИЧНИХ ПОМИЛОК ІЗ TELEGRAM-СПОВІЩЕННЯМ ---
@app.exception_handler(Exception)
async def global_exception_handler(request: Request, exc: Exception):
    error_desc = f"{type(exc).__name__}: {str(exc)} (Path: {request.url.path})"
    logger.error(f"Неопрацьована помилка: {error_desc}", exc_info=True)
    
    # Надсилаємо сповіщення в Telegram
    await send_telegram_alert(error_desc)
    
    return JSONResponse(
        status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
        content={"success": False, "detail": "Внутрішня помилка сервера."}
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