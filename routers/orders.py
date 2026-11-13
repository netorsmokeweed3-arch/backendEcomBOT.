import json as jsonlib
from typing import Optional, List, Dict, Any
from fastapi import APIRouter, HTTPException, Depends
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select
from pydantic import BaseModel

from database import get_db, StoreCredentialModel, OrderModel
from order_normalizer import normalize_shop
from server import get_current_user_id, check_rate_limit

router = APIRouter(tags=["Orders"])

class CredentialsSchema(BaseModel):
    shop: str
    platform: Optional[str] = None
    token: Optional[str] = None
    access_token: Optional[str] = None
    consumer_key: Optional[str] = None
    consumer_secret: Optional[str] = None

class OrdersResponse(BaseModel):
    success: bool
    shop: Optional[str] = None
    orders: List[Dict[str, Any]] = []
    token_expired: Optional[bool] = None
    error: Optional[str] = None


@router.post("/api/orders", response_model=OrdersResponse)
async def get_orders(payload: CredentialsSchema, user_id: int = Depends(get_current_user_id), db: AsyncSession = Depends(get_db)):
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


@router.post("/api/orders/multi")
async def get_orders_multi(user_id: int = Depends(get_current_user_id), db: AsyncSession = Depends(get_db)):
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