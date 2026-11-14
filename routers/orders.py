"""Модуль управління замовленнями, вебхуками маркетплейсів та конвеєром AI-нормалізації (Enterprise Edition - Secured & Optimized).
Інкапсулює криптографічну перевірку, захист від DoS, ідемпотентність через Redis, мультитенантність та AI Self-Healing.
"""

import hmac
import hashlib
import json as jsonlib
import logging
from typing import Optional, List, Dict, Any
from fastapi import APIRouter, HTTPException, Request, Depends, Header, status, Query
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, desc
from pydantic import BaseModel, Field, field_validator

from database import get_db, StoreCredentialModel, OrderModel, AuditLogModel
from order_normalizer import normalize_shop, normalize_order_async
from redis_pool import get_redis
from worker import save_orders_to_db
from config import WEBHOOK_SECRET
from deps import get_current_user_id, check_rate_limit

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/orders", tags=["Orders & Webhooks"])

# Максимальний розмір тіла вебхука (512 КБ) для захисту від DoS-атак пам'яті
MAX_WEBHOOK_SIZE_BYTES = 512 * 1024


# ==========================================
# 1. КРИПТОГРАФІЧНА ЗАЛЕЖНІСТЬ ВЕБХУКІВ (Security Hardened)
# ==========================================

class VerifyWebhookSignature:
    """Інкапсульована перевірка HMAC-SHA256 підпису вебхуків із захистом від переповнення пам'яті (DoS)."""
    async def __call__(self, request: Request, x_webhook_signature: Optional[str] = Header(None)) -> bytes:
        content_length = request.headers.get("content-length")
        if content_length and int(content_length) > MAX_WEBHOOK_SIZE_BYTES:
            logger.warning(f"Спроба атаки великим payload: {content_length} байтів")
            raise HTTPException(status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE, detail="Розмір вебхука перевищує допустимий ліміт.")

        body_bytes = await request.body()
        if len(body_bytes) > MAX_WEBHOOK_SIZE_BYTES:
            raise HTTPException(status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE, detail="Розмір вебхука перевищує допустимий ліміт.")
        
        if WEBHOOK_SECRET:
            if not x_webhook_signature:
                logger.warning("Спроба запиту вебхука без криптографічного підпису")
                raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Відсутній криптографічний підпис вебхука.")
            
            expected_signature = hmac.new(WEBHOOK_SECRET.encode("utf-8"), body_bytes, hashlib.sha256).hexdigest()
            if not hmac.compare_digest(expected_signature, x_webhook_signature):
                logger.warning("Критично: невірний криптографічний підпис вебхука!")
                raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Невірний криптографічний підпис.")
                
        return body_bytes


# ==========================================
# 2. ВХІДНІ ТА ВИХІДНІ СХЕМИ Pydantic v2
# ==========================================

class CredentialsSchema(BaseModel):
    shop: str = Field(..., min_length=3, max_length=255, description="Домен або назва магазину")
    platform: Optional[str] = Field(default=None, max_length=50)
    token: Optional[str] = Field(default=None, max_length=1000)
    access_token: Optional[str] = Field(default=None, max_length=1000)
    consumer_key: Optional[str] = Field(default=None, max_length=1000)
    consumer_secret: Optional[str] = Field(default=None, max_length=1000)

    @field_validator("shop")
    @classmethod
    def clean_shop_domain(cls, v: str) -> str:
        normalized = normalize_shop(v)
        return normalized if normalized else v.strip().lower()

class OrdersResponse(BaseModel):
    success: bool
    shop: Optional[str] = None
    total_count: int = 0
    orders: List[Dict[str, Any]] = []

class MultiOrdersResponse(BaseModel):
    success: bool
    total_count: int = 0
    orders: List[Dict[str, Any]] = []


# ==========================================
# 3. TENANT ISOLATION DEPENDENCY
# ==========================================

async def verify_store_ownership(
    shop_domain: str, 
    user_id: int = Depends(get_current_user_id), 
    db: AsyncSession = Depends(get_db)
) -> str:
    """Гарантує, що магазин існує і належить поточному тенанту (Tenant Isolation)."""
    normalized_shop = normalize_shop(shop_domain) or shop_domain
    stmt = select(StoreCredentialModel).where(
        StoreCredentialModel.user_id == user_id,
        StoreCredentialModel.shop_domain == normalized_shop
    )
    store = await db.scalar(stmt)
    if not store:
        logger.warning(f"Безпека: спроба доступу до чужого або неіснуючого магазину {normalized_shop} юзером {user_id}")
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Магазин не знайдено або він не належить вашому акаунту.")
    return normalized_shop


# ==========================================
# 4. МАРШРУТИЗАЦІЯ ВЕБХУКІВ ТА КОНВЕЄР НОРМАЛІЗАЦІЇ
# ==========================================

@router.post("/webhook/{platform}", status_code=status.HTTP_200_OK, tags=["Webhooks"])
async def receive_marketplace_webhook(
    platform: str, 
    body_bytes: bytes = Depends(VerifyWebhookSignature()), 
    db: AsyncSession = Depends(get_db)
) -> Dict[str, Any]:
    """Прийом вебхуків від маркетплейсів із захистом, ідемпотентністю та оркестрацією AI Self-Healing конвеєра."""
    
    event_hash = hashlib.sha256(body_bytes).hexdigest()
    platform_key = platform.lower().strip()
    idempotency_key = f"webhook:processed:{platform_key}:{event_hash}"
    
    redis = await get_redis()
    if redis:
        try:
            is_new = await redis.set(idempotency_key, "processed", nx=True, ex=300)
            if not is_new:
                logger.info(f"🔁 Отримано дублікат вебхука від {platform_key}, ідемпотентний пропуск.")
                return {"success": True, "message": "Вебхук вже було оброблено раніше."}
        except Exception as e:
            logger.error(f"Помилка Redis idempotency: {e}")

    try:
        payload = jsonlib.loads(body_bytes.decode("utf-8"))
        if not isinstance(payload, dict):
            raise ValueError("Тіло вебхука має бути JSON-об'єктом.")

        shop_domain = payload.get("shop") or payload.get("domain") or f"webhook-{platform_key}.local"
        normalized_shop_domain = normalize_shop(shop_domain) or shop_domain
        
        store_stmt = select(StoreCredentialModel).where(StoreCredentialModel.shop_domain == normalized_shop_domain)
        store_record = await db.scalar(store_stmt)
        
        if store_record:
            user_id = store_record.user_id
            raw_order = payload.get("order") or payload
            
            if isinstance(raw_order, dict) and raw_order:
                # 🚀 ОРКЕСТРАЦІЯ КОНВЕЄРА: Асинхронна нормалізація з Self-Healing
                normalized_order_data = await normalize_order_async(raw_order, normalized_shop_domain, platform_key)
                
                # Зберігаємо нормалізоване замовлення в БД через єдиний воркер
                await save_orders_to_db(db, user_id, normalized_shop_domain, [normalized_order_data])
            
            db.add(AuditLogModel(
                user_id=user_id,
                shop_domain=normalized_shop_domain,
                event_type=f"WEBHOOK_{platform_key.upper()}_PROCESSED",
                message=f"Успішно опрацьовано та нормалізовано вебхук від {platform_key} для магазину {normalized_shop_domain}"
            ))
            await db.commit()
            
        return {"success": True, "message": f"Webhook від {platform} успішно опрацьовано та нормалізовано."}
        
    except jsonlib.JSONDecodeError:
        logger.error(f"Помилка декодування JSON у вебхуку від {platform}")
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Невалідний формат JSON у тілі вебхука.")
    except Exception as e:
        await db.rollback()
        logger.error(f"Критична помилка обробки вебхука {platform}: {e}", exc_info=True)
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="Внутрішня помилка обробки вебхука.")


# ==========================================
# 5. API ЕНДПОІНТИ ОТРИМАННЯ ЗАМОВЛЕНЬ
# ==========================================

@router.post("", response_model=OrdersResponse, status_code=status.HTTP_200_OK)
async def get_orders(
    payload: CredentialsSchema, 
    limit: int = Query(50, ge=1, le=200, description="Кількість замовлень на сторінку"),
    offset: int = Query(0, ge=0, description="Зсув для пагінації"),
    user_id: int = Depends(get_current_user_id), 
    db: AsyncSession = Depends(get_db)
):
    """Отримує нормалізовані замовлення з бази даних з пагінацією та стабільним сортуванням."""
    if not await check_rate_limit(f"user:{user_id}:orders", max_requests=30):
        logger.warning(f"Перевищено ліміт запитів на отримання замовлень для user_id={user_id}")
        raise HTTPException(status_code=status.HTTP_429_TOO_MANY_REQUESTS, detail="Перевищено ліміт запитів.")

    shop = await verify_store_ownership(payload.shop, user_id, db)

    orders_stmt = (
        select(OrderModel)
        .where(OrderModel.shop_domain == shop)
        .order_by(desc(OrderModel.created_at))
        .limit(limit)
        .offset(offset)
    )
    db_orders = (await db.scalars(orders_stmt)).all()

    orders_list = []
    for o in db_orders:
        try:
            if o.raw_payload:
                parsed_payload = jsonlib.loads(o.raw_payload)
                if isinstance(parsed_payload, dict):
                    orders_list.append(parsed_payload)
                    continue
        except Exception as json_err:
            logger.debug(f"Помилка десеріалізації raw_payload для замовлення {o.external_order_id}: {json_err}")

        orders_list.append({
            "id": str(o.external_order_id),
            "order_number": str(o.order_number) if o.order_number else None,
            "customer_name": o.customer_name,
            "phone": o.phone,
            "email": o.email,
            "total_amount": {"amount": float(o.total_amount) if o.total_amount is not None else 0.0, "currency": o.currency or "UAH"},
            "currency": o.currency or "UAH",
            "status": {"text": o.status, "cancelled": False},
            "created_at": o.created_at.isoformat() if o.created_at else None,
            "shop": o.shop_domain
        })

    logger.info(f"Успішно завантажено {len(orders_list)} замовлень для магазину {shop} (user_id={user_id})")
    return {
        "success": True, 
        "shop": shop, 
        "total_count": len(orders_list),
        "orders": orders_list
    }


@router.post("/multi", response_model=MultiOrdersResponse, status_code=status.HTTP_200_OK)
async def get_orders_multi(
    limit: int = Query(100, ge=1, le=500, description="Загальний ліміт замовлень при мультизборі"),
    user_id: int = Depends(get_current_user_id), 
    db: AsyncSession = Depends(get_db)
):
    """Безпечний мультизбір замовлень з усіх підключених магазинів користувача."""
    if not await check_rate_limit(f"user:{user_id}:multi", max_requests=15):
        logger.warning(f"Перевищено ліміт мультизбору замовлень для user_id={user_id}")
        raise HTTPException(status_code=status.HTTP_429_TOO_MANY_REQUESTS, detail="Перевищено ліміт запитів для мультизбору.")

    stores_stmt = select(StoreCredentialModel).where(StoreCredentialModel.user_id == user_id)
    stores = (await db.scalars(stores_stmt)).all()
    
    if not stores:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="У вас немає підключених магазинів.")

    shop_domains = [s.shop_domain for s in stores]
    
    orders_stmt = (
        select(OrderModel)
        .where(OrderModel.shop_domain.in_(shop_domains))
        .order_by(desc(OrderModel.created_at))
        .limit(limit)
    )
    db_orders = (await db.scalars(orders_stmt)).all()

    all_orders = []
    for o in db_orders:
        try:
            if o.raw_payload:
                parsed_payload = jsonlib.loads(o.raw_payload)
                if isinstance(parsed_payload, dict):
                    all_orders.append(parsed_payload)
                    continue
        except Exception:
            pass

        all_orders.append({
            "id": str(o.external_order_id),
            "shop": o.shop_domain,
            "order_number": str(o.order_number) if o.order_number else None,
            "customer_name": o.customer_name,
            "total_amount": {"amount": float(o.total_amount) if o.total_amount is not None else 0.0, "currency": o.currency or "UAH"},
            "status": {"text": o.status, "cancelled": False},
            "currency": o.currency or "UAH",
            "created_at": o.created_at.isoformat() if o.created_at else None
        })

    logger.info(f"Мультизбір успішний: отримано {len(all_orders)} замовлень з {len(shop_domains)} магазинів для user_id={user_id}")
    return {
        "success": True, 
        "total_count": len(all_orders),
        "orders": all_orders
    }