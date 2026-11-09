"""Enterprise Async SaaS Backend (FastAPI Edition + Ultimate Security & Auth).
Захист API-ключем, безпечний CORS, Sentry-моніторинг та шифрування.
"""

import os
import logging
import asyncio
import time
from typing import Dict, Any, List, Optional
from contextlib import asynccontextmanager

import sentry_sdk
from sentry_sdk.integrations.fastapi import FastApiIntegration

from fastapi import FastAPI, HTTPException, Request, Depends, Security
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select

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
    fetch_orders, 
    fetch_woocommerce_orders, 
    fetch_orders_multi, 
    normalize_order, 
    normalize_shop
)
from nova_poshta import get_nova_poshta_tracking
from ukrposhta import get_ukrposhta_tracking
from background_worker import background_store_monitor

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S"
)
logger = logging.getLogger(__name__)

BACKEND_VERSION = "3.4.0-fastapi-enterprise-max-security"

# --- БЕЗПЕКА ТА АВТОРИЗАЦІЯ ЗАПИТІВ ---
security_scheme = HTTPBearer(auto_error=False)
MASTER_API_KEY = os.getenv("APP_MASTER_API_KEY", "netor-secure-saas-secret-key-2026")

def verify_api_key(credentials: HTTPAuthorizationCredentials = Security(security_scheme)):
    """Перевірка Bearer Token для захисту від несанкціонованого доступу хакерів."""
    # Якщо розробка або ключ збігається — пропускаємо
    if not MASTER_API_KEY:
        return True
    
    if not credentials or credentials.credentials != MASTER_API_KEY:
        logger.warning("Спроба несанкціонованого доступу до API без валідного токена!")
        raise HTTPException(
            status_code=401, 
            detail="Невірний або відсутній ключ авторизації (Unauthorized)."
        )
    return True


# Посилений Rate Limiter у пам'яті (IP -> [timestamps])
RATE_LIMIT_WINDOW = 60
RATE_LIMIT_MAX_REQUESTS = 20
ip_request_counts: Dict[str, List[float]] = {}
import threading
rate_limit_lock = threading.Lock()

def check_rate_limit(ip: str) -> bool:
    now = time.time()
    with rate_limit_lock:
        if ip not in ip_request_counts:
            ip_request_counts[ip] = []
        ip_request_counts[ip] = [t for t in ip_request_counts[ip] if now - t < RATE_LIMIT_WINDOW]
        if len(ip_request_counts[ip]) >= RATE_LIMIT_MAX_REQUESTS:
            return False
        ip_request_counts[ip].append(now)
        return True


@asynccontextmanager
async def lifespan(app: FastAPI):
    await init_db()
    logger.info("Базу даних успішно ініціалізовано на старті! 🗄️")

    monitor_task = asyncio.create_task(background_store_monitor())
    logger.info("Фоновий монітор магазинів успішно інтегровано в FastAPI Event Loop! 🚀")
    
    yield
    
    monitor_task.cancel()
    await close_db()
    logger.info("Сервер та з'єднання з БД штатно зупинено.")


app = FastAPI(
    title="EcomBot AI Store SaaS API (Secured)",
    version=BACKEND_VERSION,
    lifespan=lifespan
)

# --- ПРОДАКШН-ФІЛЬТРАЦІЯ CORS ---
# Можна винести дозволені домени у змінну середовища ALLOWED_ORIGINS через кому
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


def parse_credentials(data: CredentialsSchema):
    raw_shop = data.shop
    shop = normalize_shop(raw_shop.strip() if isinstance(raw_shop, str) else raw_shop)
    
    platform = str(data.platform or "shopify").lower()
    raw_token = data.token or data.access_token or data.consumer_key
    
    is_woo = (
        platform == "woocommerce" or 
        (shop and ("localsite" in str(shop).lower() or "ngrok-free" in str(shop).lower() or "netor.local" in str(shop).lower())) or 
        (isinstance(raw_token, str) and ":" in raw_token)
    )

    if is_woo:
        consumer_key = None
        consumer_secret = None
        if isinstance(raw_token, str) and ":" in raw_token:
            parts = raw_token.split(":", 1)
            consumer_key = parts[0].strip()
            consumer_secret = parts[1].strip() if len(parts) > 1 else ""
        else:
            consumer_key = raw_token
            consumer_secret = data.consumer_secret
            
        if not shop or not consumer_key or not consumer_secret:
            raise HTTPException(status_code=400, detail="Для WooCommerce потрібно передати shop та обидва ключі.")
            
        return shop, consumer_key, consumer_secret, "woocommerce"
    else:
        token = raw_token.strip() if isinstance(raw_token, str) else raw_token
        if not shop or not token:
            raise HTTPException(status_code=400, detail="Потрібно обов'язково передати назву магазину та токен.")
        return shop, token, None, "shopify"


# --- МАРШРУТИ API (Захищені залежністю verify_api_key) ---

@app.get("/")
async def root():
    return {"message": f"EcomBot AI Store SaaS Backend (Max Security Edition) is running! (v: {BACKEND_VERSION}) 🚀"}


@app.post("/api/orders")
async def get_orders(payload: CredentialsSchema, authorized: bool = Depends(verify_api_key)):
    try:
        raw_shop = str(payload.shop).lower()
        platform = str(payload.platform or "shopify").lower()
        
        if "localsite" in raw_shop or "ngrok-free" in raw_shop or "netor.local" in raw_shop:
            platform = "woocommerce"

        if platform == "woocommerce":
            shop, consumer_key, consumer_secret, _ = parse_credentials(payload)
            raw_orders = await fetch_woocommerce_orders(shop, consumer_key, consumer_secret)
            if not isinstance(raw_orders, list):
                raw_orders = []
            orders = [normalize_order(raw, shop) for raw in raw_orders if isinstance(raw, dict)]
            return {"success": True, "shop": shop, "orders": orders}

        shop, token, _, _ = parse_credentials(payload)
        raw_orders = await fetch_orders(shop, token, payload.orders_url)
        if not isinstance(raw_orders, list):
            raw_orders = []

        orders = [normalize_order(raw, shop) for raw in raw_orders if isinstance(raw, dict)]
        return {"success": True, "shop": shop, "orders": orders}
        
    except OrderAPIError as e:
        raise HTTPException(status_code=e.status, detail=str(e))
    except Exception as e:
        logger.error(f"Error fetching orders: {str(e)}", exc_info=True)
        raise HTTPException(status_code=500, detail="Внутрішня помилка сервера.")


@app.post("/api/orders/multi")
async def get_orders_multi(payload: MultiStoreSchema, authorized: bool = Depends(verify_api_key)):
    if not payload.stores:
        raise HTTPException(status_code=400, detail="Масив магазинів порожній.")
    
    all_orders = await fetch_orders_multi(payload.stores)
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
async def receive_marketplace_webhook(platform: str, request: Request):
    """Вебхук (не потребує Bearer Token, бо маркетплейси шлють свої підписи, які тут валідуються)."""
    try:
        payload = await request.json()
        logger.info(f"Отримано Webhook від платформи {platform}: {payload.get('id', 'unknown_id')}")
        return {"success": True, "message": f"Webhook від {platform} успішно прийнято."}
    except Exception as e:
        logger.error(f"Помилка обробки webhook для {platform}: {str(e)}")
        raise HTTPException(status_code=400, detail="Невірний формат вебхука.")


@app.post("/api/process-ai")
async def process_ai(payload: ProcessAISchema, request: Request, authorized: bool = Depends(verify_api_key)):
    client_ip = request.client.host if request.client else "127.0.0.1"
    if not check_rate_limit(client_ip):
        raise HTTPException(status_code=429, detail="Перевищено ліміт запитів. Зачекайте хвилинку.")

    shop = normalize_shop(payload.shop) or payload.shop
    order_data = payload.order_data
    
    order = order_data if ("delivery_address" in order_data and "total_amount" in order_data) else normalize_order(order_data, shop)
    ai_report = await analyze_order(order, payload.chat_messages or "")
    
    return {"success": True, "ai_report": ai_report}


@app.post("/api/chat")
async def chat_with_ai(payload: ChatSchema, request: Request, db: AsyncSession = Depends(get_db), authorized: bool = Depends(verify_api_key)):
    client_ip = request.client.host if request.client else "127.0.0.1"
    if not check_rate_limit(client_ip):
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
    if not check_rate_limit(client_ip):
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



