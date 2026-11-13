import os
import logging
import asyncio
import time
import uuid
from typing import Dict, List
from contextlib import asynccontextmanager
from contextvars import ContextVar
from pathlib import Path

if Path(".env").exists():
    from dotenv import load_dotenv
    load_dotenv()

import sentry_sdk
from sentry_sdk.integrations.fastapi import FastApiIntegration

from fastapi import FastAPI, Request
from fastapi.security import HTTPBearer
from fastapi.middleware.cors import CORSMiddleware

# Ініціалізація Sentry
SENTRY_DSN = os.getenv("SENTRY_DSN", "")
if SENTRY_DSN:
    sentry_sdk.init(
        dsn=SENTRY_DSN,
        integrations=[FastApiIntegration()],
        traces_sample_rate=1.0,
        environment=os.getenv("ENVIRONMENT", "production")
    )

from database import init_db, close_db, AsyncSessionLocal
from redis_pool import get_redis, close_redis
from clients.dispatcher import MarketplaceDispatcher
from background_worker import background_store_monitor

# Підключаємо роутери
from routers.auth import router as auth_router
from routers.stores import router as stores_router
from routers.orders import router as orders_router
from routers.ai_support import router as ai_support_router

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

memory_rate_limit: Dict[str, List[float]] = {}
rate_limit_lock = asyncio.Lock()

JWT_SECRET_KEY = os.getenv("JWT_SECRET_KEY")
if not JWT_SECRET_KEY:
    raise RuntimeError("КРИТИЧНА ПОМИЛКА БЕЗПЕКИ: JWT_SECRET_KEY не задана!")

JWT_ALGORITHM = "HS256"
JWT_ACCESS_EXPIRATION_MINUTES = 15
JWT_REFRESH_EXPIRATION_DAYS = 7

security_scheme = HTTPBearer(auto_error=False)
WEBHOOK_SECRET = os.getenv("WEBHOOK_SECRET", "")

@asynccontextmanager
async def lifespan(app: FastAPI):
    redis = await get_redis()
    if redis:
        try:
            await redis.ping()
            logger.info("OmniFlow: Підключено до Redis! ⚡")
        except Exception as e:
            logger.error(f"Помилка Redis: {e}")

    await init_db()
    logger.info("OmniFlow: Базу даних ініціалізовано! 🗄️")

    bg_worker_task = asyncio.create_task(background_store_monitor())
    logger.info("OmniFlow: Background store monitor запущено! 🚀")

    yield
    
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

# --- ПІДКЛЮЧЕННЯ РОУТЕРІВ ---
app.include_router(auth_router)
app.include_router(stores_router)
app.include_router(orders_router)
app.include_router(ai_support_router)

if __name__ == "__main__":
    import uvicorn
    uvicorn.run("server:app", host="0.0.0.0", port=5000, reload=True)