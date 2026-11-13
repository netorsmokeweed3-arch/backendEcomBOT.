import json as jsonlib
import logging
from typing import Optional, List, Dict, Any
from fastapi import APIRouter, HTTPException, Depends, status, Query
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, desc
from pydantic import BaseModel, Field, field_validator

from database import get_db, StoreCredentialModel, OrderModel
from order_normalizer import normalize_shop
from server import get_current_user_id, check_rate_limit

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/orders", tags=["Orders"])

# --- ВХІДНІ ТА ВИХІДНІ СХЕМИ ---
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

class OrderOutSchema(BaseModel):
    id: str
    order_number: Optional[str] = None
    customer_name: Optional[str] = None
    phone: Optional[str] = None
    email: Optional[str] = None
    total_amount: Optional[float] = None
    currency: Optional[str] = "UAH"
    status: Optional[str] = None
    created_at: Optional[str] = None
    shop: Optional[str] = None

class OrdersResponse(BaseModel):
    success: bool
    shop: Optional[str] = None
    total_count: int = 0
    orders: List[OrderOutSchema] = []
    token_expired: Optional[bool] = None
    error: Optional[str] = None

class MultiOrdersResponse(BaseModel):
    success: bool
    total_count: int = 0
    orders: List[OrderOutSchema] = []


# --- PRINCIPAL DEPENDENCY: Ізольована перевірка та валідація магазину ---
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


@router.post("", response_model=OrdersResponse, status_code=status.HTTP_200_OK)
async def get_orders(
    payload: CredentialsSchema, 
    limit: int = Query(50, ge=1, le=200, description="Кількість замовлень на сторінку"),
    offset: int = Query(0, ge=0, description="Зсув для пагінації"),
    user_id: int = Depends(get_current_user_id), 
    db: AsyncSession = Depends(get_db)
):
    """Отримує замовлення з персистентної бази даних з пагінацією та стабільним сортуванням."""
    if not await check_rate_limit(f"user:{user_id}:orders", max_requests=30):
        logger.warning(f"Перевищено ліміт запитів на отримання замовлень для user_id={user_id}")
        raise HTTPException(status_code=status.HTTP_429_TOO_MANY_REQUESTS, detail="Перевищено ліміт запитів.")

    # Використовуємо інкапсульовану перевірку право власності
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
            "total_amount": float(o.total_amount) if o.total_amount is not None else 0.0,
            "currency": o.currency or "UAH",
            "status": o.status,
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
    """Безпечний мультизбір замовлень з усіх магазинів користувача."""
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
            "total_amount": float(o.total_amount) if o.total_amount is not None else 0.0,
            "status": o.status,
            "currency": o.currency or "UAH",
            "created_at": o.created_at.isoformat() if o.created_at else None
        })

    logger.info(f"Мультизбір успішний: отримано {len(all_orders)} замовлень з {len(shop_domains)} магазинів для user_id={user_id}")
    return {
        "success": True, 
        "total_count": len(all_orders),
        "orders": all_orders
    }