import hmac
import hashlib
import json as jsonlib
import logging
from typing import Optional, Dict, Any

from fastapi import APIRouter, HTTPException, Request, Depends, Header, status
from fastapi.responses import JSONResponse
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select
from pydantic import BaseModel, Field

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
from config import BACKEND_VERSION, WEBHOOK_SECRET
from deps import check_rate_limit, get_current_user_id

logger = logging.getLogger(__name__)

router = APIRouter(tags=["AI & Operations"])

# --- PRINCIPAL PATTERN: Декларативна криптографічна перевірка вебхуків ---
class VerifyWebhookSignature:
    """Інкапсульована залежність для перевірки HMAC-SHA256 підпису маркетплейсів (DRY & Security)."""
    async def __call__(self, request: Request, x_webhook_signature: Optional[str] = Header(None)) -> bytes:
        body_bytes = await request.body()
        
        if WEBHOOK_SECRET:
            if not x_webhook_signature:
                logger.warning("Спроба запиту вебхука без криптографічного підпису")
                raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Відсутній криптографічний підпис вебхука.")
            
            expected_signature = hmac.new(WEBHOOK_SECRET.encode("utf-8"), body_bytes, hashlib.sha256).hexdigest()
            if not hmac.compare_digest(expected_signature, x_webhook_signature):
                logger.warning("Критично: невірний криптографічний підпис вебхука!")
                raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Невірний криптографічний підпис.")
                
        return body_bytes


# --- ВХІДНІ СХЕМИ Pydantic v2 ---
class TrackingSchema(BaseModel):
    trackingNumber: str = Field(..., min_length=1, max_length=100, description="Номер ТТН")
    carrier: Optional[str] = Field(default="Нова Пошта", max_length=50)

class ProcessAISchema(BaseModel):
    shop: str = Field(..., min_length=3, max_length=255)
    token: Optional[str] = Field(default=None, max_length=1000)
    order_data: Dict[str, Any] = Field(...)
    chat_messages: Optional[str] = Field(default="", max_length=5000)

class ChatSchema(BaseModel):
    shop: str = Field(..., min_length=3, max_length=255)
    token: Optional[str] = Field(default=None, max_length=1000)
    message: str = Field(..., min_length=1, max_length=2000)
    chat_id: Optional[str] = Field(default="default_chat", max_length=100)

class ChatModeSchema(BaseModel):
    chat_id: str = Field(..., min_length=1, max_length=100)
    mode: str = Field(..., pattern="^(ai|manual)$")

class SupportSchema(BaseModel):
    shop: str = Field(..., min_length=3, max_length=255)
    token: Optional[str] = Field(default=None, max_length=1000)
    complaint: Optional[str] = Field(default=None, max_length=2000)
    message: Optional[str] = Field(default=None, max_length=2000)
    order_info: Optional[Dict[str, Any]] = Field(default_factory=dict)

class PushTokenSchema(BaseModel):
    push_token: str = Field(..., min_length=10, max_length=500)


@router.get("/", status_code=status.HTTP_200_OK, tags=["System"])
async def root() -> Dict[str, str]:
    return {"message": f"OmniFlow Enterprise SaaS Backend is running! (v: {BACKEND_VERSION}) 🚀"}


@router.get("/health", status_code=status.HTTP_200_OK, tags=["System"])
async def health_check() -> JSONResponse:
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


@router.post("/api/tracking", status_code=status.HTTP_200_OK, tags=["Logistics"])
async def track_order_parcel(
    payload: TrackingSchema, 
    user_id: int = Depends(get_current_user_id)
) -> Any:
    if not await check_rate_limit(f"user:{user_id}:tracking", max_requests=15, window_seconds=60):
        raise HTTPException(status_code=status.HTTP_429_TOO_MANY_REQUESTS, detail="Перевищено ліміт запитів на відстеження посилок.")

    ttn = payload.trackingNumber.strip()
    carrier = payload.carrier.strip() if payload.carrier else "Нова Пошта"
    if not ttn:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Не вказано номер ТТН.")
        
    if "укр" in carrier.lower():
        return await get_ukrposhta_tracking(ttn)
    return await get_nova_poshta_tracking(ttn)


@router.post("/api/webhook/{platform}", status_code=status.HTTP_200_OK, tags=["Webhooks"])
async def receive_marketplace_webhook(
    platform: str, 
    body_bytes: bytes = Depends(VerifyWebhookSignature()), 
    db: AsyncSession = Depends(get_db)
) -> Dict[str, Any]:
    """Опрацювання вебхуків з інкапсульованою криптографічною перевіркою та ідемпотентністю."""
    event_hash = hashlib.sha256(body_bytes).hexdigest()
    idempotency_key = f"webhook:processed:{platform}:{event_hash}"
    
    redis = await get_redis()
    if redis:
        try:
            is_new = await redis.set(idempotency_key, "processed", nx=True, ex=300)
            if not is_new:
                return {"success": True, "message": "Вебхук вже було оброблено раніше."}
        except Exception as e:
            logger.error(f"Помилка Redis idempotency: {e}")

    try:
        payload = jsonlib.loads(body_bytes.decode("utf-8"))
        shop_domain = payload.get("shop") or payload.get("domain") or f"webhook-{platform}.local"
        normalized_shop_domain = normalize_shop(shop_domain) or shop_domain
        
        store_stmt = select(StoreCredentialModel).where(StoreCredentialModel.shop_domain == normalized_shop_domain)
        store_record = await db.scalar(store_stmt)
        
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
        logger.error(f"Помилка парсингу вебхука {platform}: {e}")
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Невірний формат вебхука.")


@router.post("/api/process-ai", status_code=status.HTTP_200_OK, tags=["AI & Chat"])
async def process_ai(
    payload: ProcessAISchema, 
    user_id: int = Depends(get_current_user_id)
) -> Dict[str, Any]:
    if not await check_rate_limit(f"user:{user_id}:ai", max_requests=20):
        raise HTTPException(status_code=status.HTTP_429_TOO_MANY_REQUESTS, detail="Перевищено ліміт запитів до ШІ.")

    shop = normalize_shop(payload.shop) or payload.shop
    order = payload.order_data if ("delivery_address" in payload.order_data) else normalize_order(payload.order_data, shop)
    
    ai_report = await analyze_order(order, payload.chat_messages or "", user_id=user_id, shop_domain=shop)
    return {"success": True, "ai_report": ai_report}


@router.post("/api/chat", status_code=status.HTTP_200_OK, tags=["AI & Chat"])
async def chat_with_ai(
    payload: ChatSchema, 
    user_id: int = Depends(get_current_user_id), 
    db: AsyncSession = Depends(get_db)
) -> Dict[str, Any]:
    if not await check_rate_limit(f"user:{user_id}:chat", max_requests=30):
        raise HTTPException(status_code=status.HTTP_429_TOO_MANY_REQUESTS, detail="Перевищено ліміт запитів до чату.")

    chat_id = payload.chat_id or "default_chat"
    shop = normalize_shop(payload.shop) or payload.shop
    
    mode_stmt = select(ChatModeModel).where(
        ChatModeModel.chat_id == chat_id,
        ChatModeModel.user_id == user_id
    )
    chat_mode_record = await db.scalar(mode_stmt)
    current_mode = chat_mode_record.mode if chat_mode_record else "ai"

    if current_mode == "manual":
        return {"success": True, "reply": "", "mode": "manual", "info": "Чат у ручному режимі."}

    dummy_order = {"id": "chat-session", "customer_name": "Покупець у чаті", "items": []}
    ai_report = await analyze_order(dummy_order, payload.message, user_id=user_id, shop_domain=shop)
    ai_reply = ai_report.get("recommendation") or ai_report.get("ai_summary") or "Вітаю! Чим можу допомогти?"

    return {"success": True, "reply": ai_reply, "mode": "ai"}


@router.post("/api/chat/mode", status_code=status.HTTP_200_OK, tags=["AI & Chat"])
async def set_chat_mode(
    payload: ChatModeSchema, 
    user_id: int = Depends(get_current_user_id), 
    db: AsyncSession = Depends(get_db)
) -> Dict[str, Any]:
    mode_stmt = select(ChatModeModel).where(
        ChatModeModel.chat_id == payload.chat_id,
        ChatModeModel.user_id == user_id
    )
    chat_mode_record = await db.scalar(mode_stmt)

    if chat_mode_record:
        chat_mode_record.mode = payload.mode
    else:
        db.add(ChatModeModel(chat_id=payload.chat_id, user_id=user_id, mode=payload.mode))

    await db.commit()
    return {"success": True, "chat_id": payload.chat_id, "mode": payload.mode}


@router.post("/api/support", status_code=status.HTTP_200_OK, tags=["Support"])
async def support_and_returns(
    payload: SupportSchema, 
    user_id: int = Depends(get_current_user_id)
) -> Dict[str, Any]:
    """Обробка скарг та запитів підтримки за допомогою агента (синтаксис виправлено)."""
    if not await check_rate_limit(f"user:{user_id}:support", max_requests=20):
        raise HTTPException(status_code=status.HTTP_429_TOO_MANY_REQUESTS, detail="Перевищено ліміт запитів.")

    complaint_text = payload.complaint or payload.message
    if not complaint_text:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Текст скарги не може бути порожнім.")

    support_analysis = await MultiAgentSystem.run_support_agent(complaint_text, payload.order_info or {})
    return {"success": True, "support_report": support_analysis}


@router.post("/api/notifications/register", status_code=status.HTTP_200_OK, tags=["Notifications"])
async def register_push_token(
    payload: PushTokenSchema, 
    user_id: int = Depends(get_current_user_id), 
    db: AsyncSession = Depends(get_db)
) -> Dict[str, Any]:
    token_stmt = select(PushTokenModel).where(PushTokenModel.token == payload.push_token)
    existing = await db.scalar(token_stmt)
    
    if existing:
        existing.user_id = user_id
    else:
        db.add(PushTokenModel(user_id=user_id, token=payload.push_token))
        
    await db.commit()
    return {"success": True, "message": "Push-токен зареєстровано."}