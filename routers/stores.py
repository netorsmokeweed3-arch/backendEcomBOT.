import json as jsonlib
from typing import Optional, List, Dict, Any
from fastapi import APIRouter, HTTPException, Depends
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select
from pydantic import BaseModel

from database import get_db, StoreCredentialModel, encrypt_credential, decrypt_credential
from order_normalizer import normalize_shop
from server import get_current_user_id

router = APIRouter(tags=["Stores"])

class StoreCreateSchema(BaseModel):
    shop: str
    platform: str = "prom"
    token: Optional[str] = None
    access_token: Optional[str] = None
    consumer_key: Optional[str] = None
    consumer_secret: Optional[str] = None


@router.post("/api/stores")
async def add_store(payload: StoreCreateSchema, user_id: int = Depends(get_current_user_id), db: AsyncSession = Depends(get_db)):
    shop = normalize_shop(payload.shop)
    if not shop:
        raise HTTPException(status_code=400, detail="Некоректний домен магазину.")
    
    credentials = {
        "token": payload.token or payload.access_token or "",
        "consumer_key": payload.consumer_key or "",
        "consumer_secret": payload.consumer_secret or "",
    }
    if not any(credentials.values()):
        raise HTTPException(status_code=400, detail="Не передано жодного токена або ключа доступу.")
    
    encrypted = encrypt_credential(jsonlib.dumps(credentials))
    
    result = await db.execute(select(StoreCredentialModel).where(StoreCredentialModel.shop_domain == shop))
    existing = result.scalars().first()
    if existing:
        if existing.user_id != user_id:
            raise HTTPException(status_code=403, detail="Цей магазин вже підключено іншим акаунтом.")
        existing.platform = payload.platform
        existing.encrypted_credentials = encrypted
    else:
        db.add(StoreCredentialModel(
            user_id=user_id,
            shop_domain=shop,
            platform=payload.platform,
            encrypted_credentials=encrypted,
        ))
    await db.commit()
    return {"success": True, "shop": shop, "platform": payload.platform}


@router.get("/api/stores")
async def list_stores(user_id: int = Depends(get_current_user_id), db: AsyncSession = Depends(get_db)):
    result = await db.execute(select(StoreCredentialModel).where(StoreCredentialModel.user_id == user_id))
    stores = result.scalars().all()
    return {
        "success": True,
        "stores": [{"id": s.id, "shop": s.shop_domain, "platform": s.platform} for s in stores]
    }


@router.delete("/api/stores/{store_id}")
async def delete_store(store_id: int, user_id: int = Depends(get_current_user_id), db: AsyncSession = Depends(get_db)):
    result = await db.execute(select(StoreCredentialModel).where(
        StoreCredentialModel.id == store_id,
        StoreCredentialModel.user_id == user_id,
    ))
    store = result.scalars().first()
    if not store:
        raise HTTPException(status_code=404, detail="Магазин не знайдено.")
    await db.delete(store)
    await db.commit()
    return {"success": True}
