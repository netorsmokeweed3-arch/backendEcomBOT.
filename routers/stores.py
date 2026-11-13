import json as jsonlib
import logging
from typing import Optional, List

from fastapi import APIRouter, HTTPException, Depends, status
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select
from pydantic import BaseModel, Field, field_validator

from database import get_db, StoreCredentialModel, encrypt_credential
from order_normalizer import normalize_shop
from server import get_current_user_id

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/stores", tags=["Stores"])

# --- ВХІДНІ СХЕМИ (Request Payloads) ---
class StoreCreateSchema(BaseModel):
    shop: str = Field(..., min_length=3, max_length=255, description="Домен або назва магазину")
    platform: str = Field(default="prom", min_length=2, max_length=50, description="Платформа маркетплейсу")
    token: Optional[str] = Field(default=None, max_length=1000)
    access_token: Optional[str] = Field(default=None, max_length=1000)
    consumer_key: Optional[str] = Field(default=None, max_length=1000)
    consumer_secret: Optional[str] = Field(default=None, max_length=1000)

    @field_validator("shop")
    @classmethod
    def clean_shop_domain(cls, v: str) -> str:
        """Автоматична очистка домену на рівні Pydantic валідації."""
        normalized = normalize_shop(v)
        return normalized if normalized else v.strip().lower()


# --- ВИХІДНІ СХЕМИ (Response Models для Swagger) ---
class StoreCreatedResponse(BaseModel):
    success: bool
    shop: str
    platform: str

class StoreItemOut(BaseModel):
    id: int
    shop: str
    platform: str

class StoreListResponse(BaseModel):
    success: bool
    stores: List[StoreItemOut]

class StandardSuccessResponse(BaseModel):
    success: bool


@router.post("", response_model=StoreCreatedResponse, status_code=status.HTTP_201_CREATED)
async def add_store(
    payload: StoreCreateSchema, 
    user_id: int = Depends(get_current_user_id), 
    db: AsyncSession = Depends(get_db)
):
    """Створює або оновлює креденшали магазину в зашифрованому вигляді (AES/Fernet) з перевіркою тенанта."""
    shop = normalize_shop(payload.shop)
    if not shop:
        logger.warning(f"Спроба додати магазин з некоректним доменом для user_id={user_id}")
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Некоректний домен магазину.")
    
    credentials = {
        "token": payload.token or payload.access_token or "",
        "consumer_key": payload.consumer_key or "",
        "consumer_secret": payload.consumer_secret or "",
    }
    
    active_credentials = {k: v for k, v in credentials.items() if v}
    if not active_credentials:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Не передано жодного токена або ключа доступу.")
    
    try:
        encrypted_data = encrypt_credential(jsonlib.dumps(active_credentials))
    except Exception as e:
        logger.error(f"Помилка шифрування креденшалів для магазину {shop}: {e}")
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="Помилка безпеки при шифруванні даних.")
    
    try:
        stmt = select(StoreCredentialModel).where(StoreCredentialModel.shop_domain == shop)
        existing_store = await db.scalar(stmt)
        
        if existing_store:
            if existing_store.user_id != user_id:
                logger.warning(f"Безпека: юзер {user_id} намагався перезаписати магазин {shop}, який належить юзеру {existing_store.user_id}")
                raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Цей магазин вже підключено іншим акаунтом.")
            
            existing_store.platform = payload.platform
            existing_store.encrypted_credentials = encrypted_data
            logger.info(f"Оновлено креденшали для магазину {shop} (user_id={user_id})")
        else:
            new_store = StoreCredentialModel(
                user_id=user_id,
                shop_domain=shop,
                platform=payload.platform,
                encrypted_credentials=encrypted_data,
            )
            db.add(new_store)
            logger.info(f"Створено новий підключений магазин {shop} для user_id={user_id}")
            
        await db.commit()
        return {"success": True, "shop": shop, "platform": payload.platform}
        
    except HTTPException:
        await db.rollback()
        raise
    except Exception as e:
        await db.rollback()
        logger.error(f"Помилка бази даних при збереженні магазину {shop}: {e}")
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="Помилка збереження магазину в базу даних.")


@router.get("", response_model=StoreListResponse, status_code=status.HTTP_200_OK)
async def list_stores(
    user_id: int = Depends(get_current_user_id), 
    db: AsyncSession = Depends(get_db)
):
    """Повертає список підключених магазинів поточного користувача без витоку секретних токенів."""
    try:
        stmt = select(StoreCredentialModel).where(StoreCredentialModel.user_id == user_id)
        stores = (await db.scalars(stmt)).all()
        
        return {
            "success": True,
            "stores": [{"id": s.id, "shop": s.shop_domain, "platform": s.platform} for s in stores]
        }
    except Exception as e:
        logger.error(f"Помилка отримання списку магазинів для user_id={user_id}: {e}")
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="Не вдалося отримати список магазинів.")


@router.delete("/{store_id}", response_model=StandardSuccessResponse, status_code=status.HTTP_200_OK)
async def delete_store(
    store_id: int, 
    user_id: int = Depends(get_current_user_id), 
    db: AsyncSession = Depends(get_db)
):
    """Безпечно видаляє підключений магазин із суворою перевіркою належності користувачу (Tenant Isolation)."""
    try:
        stmt = select(StoreCredentialModel).where(
            StoreCredentialModel.id == store_id,
            StoreCredentialModel.user_id == user_id,
        )
        store = await db.scalar(stmt)
        
        if not store:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Магазин не знайдено.")
            
        await db.delete(store)
        await db.commit()
        
        logger.info(f"Магазин id={store_id} ({store.shop_domain}) успішно видалено користувачем user_id={user_id}")
        return {"success": True}
        
    except HTTPException:
        await db.rollback()
        raise
    except Exception as e:
        await db.rollback()
        logger.error(f"Помилка видалення магазину id={store_id}: {e}")
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="Не вдалося видалити магазин.")