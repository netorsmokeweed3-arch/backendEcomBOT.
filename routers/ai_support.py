import hmac
import hashlib
from typing import Optional, Dict, Any
from fastapi import APIRouter, HTTPException, Request, Depends, Header
from fastapi.responses import JSONResponse
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select
from pydantic import BaseModel

from database import (
    AsyncSessionLocal, get_db, ChatModeModel, PushTokenModel, 
    AuditLogModel, StoreCredentialModel
)
from redis_pool import get_redis
from ai_service import analyze_order
from agents import MultiAgentSystem
from order_normalizer import normalize_shop, normalize_order
from nova_poshta import get_nova_poshta_tracking
from ukrposhta import get_ukrposhta_tracking
from background_worker import save_orders_to_db
from server import (
    BACKEND_VERSION, WEBHOOK_SECRET, check_rate_limit, get_current_user_id
)

router = APIRouter()

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


@router.get("/", tags=["System"])
async def root():
    return {"message": f"OmniFlow Enterprise SaaS Backend is running! (v: {BACKEND_VERSION}) 🚀"}


@router.get("/health", tags=["System"])
async def health_check():
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


@router.post("/api/tracking", tags=["Logistics"])
async def track_order_parcel(payload: TrackingSchema, user_id: int = Depends(get_current_user_id)):
    if not await check_rate_limit(f"user:{user_id}:tracking", max_requests=15, window_seconds=60):
        raise HTTPException(status_code=429, detail="Перевищено ліміт запитів на відстеження посилок.")

    ttn = payload.trackingNumber.strip()
    carrier = payload.carrier.strip() if payload.carrier else "Нова Пошта"
    if not ttn:
        raise HTTPException(status_code=400, detail="Не вказано номер ТТН.")
        
    if "укр" in carrier.lower():
        return await get_ukrposhta_tracking(ttn)
    return await get_nova_poshta_tracking(ttn)


@router.post("/api/webhook/{platform}", tags=["Webhooks"])
async def receive_marketplace_webhook(platform: str, request: Request, x_webhook_signature: Optional[str] = Header(None), db: AsyncSession = Depends(get_db)):
    body_bytes = await request.body()
    
    if WEBHOOK_SECRET:
        if not x_webhook_signature:
            raise HTTPException(status_code=403, detail="Відсутній криптографічний підпис вебхука.")
        expected_signature = hmac.new(WEBHOOK_SECRET.encode("utf-8"), body_bytes, hashlib.sha256).hexdigest()
        if not hmac.compare_digest(expected_signature, x_webhook_signature):
            raise HTTPException(status_code=403, detail="Невірний криптографічний підпис.")

    event_hash = hashlib.sha256(body_bytes).hexdigest()
    idempotency_key = f"webhook:processed:{platform}:{event_hash}"
    
    redis = await get_redis()
    if redis:
        try:
            is_new = await redis.set(idempotency_key, "processed", nx=True, ex=300)
            if not is_new:
                return {"success": True, "message": "Вебхук вже було оброблено раніше."}
        except Exception:
            pass

    try:
        payload = await request.json()
        shop_domain = payload.get("shop") or payload.get("domain") or f"webhook-{platform}.local"
        normalized_shop_domain = normalize_shop(shop_domain) or shop_domain
        
        store_result = await db.execute(select(StoreCredentialModel).where(StoreCredentialModel.shop_domain == normalized_shop_domain))
        store_record = store_result.scalars().first()
        
        if store_record:
            user_id = store_record.user_id
            order_data = payload.get("order") or payload
            if isinstance(order_data, dict) and ("id" in order_data or "order_number" in order_data):
                await save_orders_to_db(db, user_id, normalized_shop_domain, [order_data])
            
            db.add(AuditLogModel(
                user_id=user_id,
                shop_domain=normalized_shop_domain,
                event_type=f"WEBHOOK_{platform.upper()}_RECEIVED",
                message=f"Успішно опрацьовано вебхук для магазину {normalized_shop_domain}"
            ))
            await db.commit()
            
        return {"success": True, "message": f"Webhook від {platform} успішно опрацьовано."}
    except Exception as e:
        raise HTTPException(status_code=400, detail="Невірний формат вебхука.")


@router.post("/api/process-ai", tags=["AI & Chat"])
async def process_ai(payload: ProcessAISchema, user_id: int = Depends(get_current_user_id)):
    if not await check_rate_limit(f"user:{user_id}:ai", max_requests=20):
        raise HTTPException(status_code=429, detail="Перевищено ліміт запитів до ШІ.")

    shop = normalize_shop(payload.shop) or payload.shop
    order = payload.order_data if ("delivery_address" in payload.order_data) else normalize_order(payload.order_data, shop)
    
    ai_report = await analyze_order(order, payload.chat_messages or "", user_id=user_id, shop_domain=shop)
    return {"success": True, "ai_report": ai_report}


@router.post("/api/chat", tags=["AI & Chat"])
async def chat_with_ai(payload: ChatSchema, user_id: int = Depends(get_current_user_id), db: AsyncSession = Depends(get_db)):
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


@router.post("/api/chat/mode", tags=["AI & Chat"])
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


@router.post("/api/support", tags=["Support"])
async def support_and_returns(payload: SupportSchema, user_id: int = Depends(get_current_user_id)):
    if not await check_rate_limit(f"user:{user_id}:support", max_requests=20):
        raise HTTPException(status_code=429, detail="Перевищено ліміт запитів.")

    complaint_text = payload.complaint or payload.message
    if not complaint_text:
        raise HTTPException(status_code=400, detail="Текст скарги не може бути порожнім.")

    support_analysis = await MultiAgentSystem.run_support_agent(complaint_text, payload.order_info or {})
    return {"success": True, "support_report": support_analysis}


@router.post("/api/notifications/register", tags=["Notifications"])
async def register_push_token(payload: PushTokenSchema, user_id: int = Depends(get_current_user_id), db: AsyncSession = Depends(get_db)):
    result = await db.execute(select(PushTokenModel).where(PushTokenModel.token == payload.push_token))
    existing = result.scalars().first()
    if existing:
        existing.user_id = user_id
    else:
        db.add(PushTokenModel(user_id=user_id, token=payload.push_token))
    await db.commit()
            
    return {"success": True, "message": "Push-токен зареєстровано."}