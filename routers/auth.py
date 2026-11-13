import os
import time
import uuid
import logging
import asyncio  
from datetime import datetime, timezone, timedelta
from typing import Optional

from fastapi import APIRouter, HTTPException, Request, Depends, status
from fastapi.security import HTTPAuthorizationCredentials
from pydantic import BaseModel, EmailStr, field_validator
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
import jwt
import bcrypt

from database import AsyncSessionLocal, get_db, UserModel
from server import (
    JWT_SECRET_KEY, JWT_ALGORITHM, JWT_ACCESS_EXPIRATION_MINUTES, JWT_REFRESH_EXPIRATION_DAYS,
    security_scheme, check_rate_limit, get_current_user_id, get_redis
)

logger = logging.getLogger(__name__)
router = APIRouter(tags=["Auth"])

# --- СХЕМИ Pydantic ---
class UserRegisterSchema(BaseModel):
    email: EmailStr
    password: str

    @field_validator("password")
    @classmethod
    def validate_password(cls, v: str) -> str:
        if len(v) < 8:
            raise ValueError("Пароль має містити щонайменше 8 символів.")
        if len(v.encode('utf-8')) > 72:
            raise ValueError("Пароль занадто довгий (максимум 72 байти для bcrypt).")
        return v

class UserLoginSchema(BaseModel):
    email: EmailStr
    password: str

class RefreshTokenSchema(BaseModel):
    refresh_token: str

class LogoutSchema(BaseModel):
    refresh_token: Optional[str] = None

class AuthUserOut(BaseModel):
    id: int
    email: str

class AuthResponse(BaseModel):
    success: bool
    access_token: str
    refresh_token: str
    user: AuthUserOut

class TokenRefreshResponse(BaseModel):
    success: bool
    access_token: str
    refresh_token: str

class StandardStatusResponse(BaseModel):
    success: bool
    message: Optional[str] = None

# Допоміжні функції хешування та токенів
def _hash_password_sync(password: str) -> str:
    return bcrypt.hashpw(password.encode('utf-8'), bcrypt.gensalt()).decode('utf-8')

def _verify_password_sync(plain_password: str, hashed_password: str) -> bool:
    return bcrypt.checkpw(plain_password.encode('utf-8'), hashed_password.encode('utf-8'))

async def hash_password(password: str) -> str:
    return await asyncio.to_thread(_hash_password_sync, password)

async def verify_password(plain_password: str, hashed_password: str) -> bool:
    return await asyncio.to_thread(_verify_password_sync, plain_password, hashed_password)

def create_access_token(data: dict) -> str:
    to_encode = data.copy()
    expire = datetime.now(timezone.utc) + timedelta(minutes=JWT_ACCESS_EXPIRATION_MINUTES)
    to_encode.update({"exp": expire, "type": "access", "jti": str(uuid.uuid4())})
    return jwt.encode(to_encode, JWT_SECRET_KEY, algorithm=JWT_ALGORITHM)

def create_refresh_token(data: dict) -> str:
    to_encode = data.copy()
    expire = datetime.now(timezone.utc) + timedelta(days=JWT_REFRESH_EXPIRATION_DAYS)
    to_encode.update({"exp": expire, "type": "refresh", "jti": str(uuid.uuid4())})
    return jwt.encode(to_encode, JWT_SECRET_KEY, algorithm=JWT_ALGORITHM)


@router.post("/api/auth/register", response_model=AuthResponse)
async def register(payload: UserRegisterSchema, request: Request, db: AsyncSession = Depends(get_db)):
    client_ip = request.client.host if request.client else "127.0.0.1"
    if not await check_rate_limit(f"reg:{client_ip}", max_requests=5, window_seconds=60):
        raise HTTPException(status_code=429, detail="Забагато спроб реєстрації. Зачекайте.")

    result = await db.execute(select(UserModel).where(UserModel.email == payload.email))
    if result.scalars().first():
        raise HTTPException(status_code=400, detail="Користувач із такою електронною поштою вже існує.")
    
    hashed_pwd = await hash_password(payload.password)
    new_user = UserModel(email=payload.email, hashed_password=hashed_pwd)
    db.add(new_user)
    try:
        await db.commit()
    except IntegrityError:
        await db.rollback()
        raise HTTPException(status_code=400, detail="Користувач із такою електронною поштою вже існує.")
    except Exception:
        await db.rollback()
        raise HTTPException(status_code=400, detail="Помилка реєстрації користувача.")
    await db.refresh(new_user)
    
    access_token = create_access_token({"sub": str(new_user.id), "email": new_user.email})
    refresh_token = create_refresh_token({"sub": str(new_user.id)})
    
    return {
        "success": True, 
        "access_token": access_token, 
        "refresh_token": refresh_token,
        "user": {"id": new_user.id, "email": new_user.email}
    }


@router.post("/api/auth/login", response_model=AuthResponse)
async def login(payload: UserLoginSchema, request: Request, db: AsyncSession = Depends(get_db)):
    client_ip = request.client.host if request.client else "127.0.0.1"
    if not await check_rate_limit(f"login:{client_ip}", max_requests=10, window_seconds=60):
        raise HTTPException(status_code=429, detail="Забагато спроб входу. Зачекайте.")

    result = await db.execute(select(UserModel).where(UserModel.email == payload.email))
    user = result.scalars().first()
    
    if not user or not await verify_password(payload.password, user.hashed_password):
        raise HTTPException(status_code=401, detail="Невірний email або пароль.")
    
    access_token = create_access_token({"sub": str(user.id), "email": user.email})
    refresh_token = create_refresh_token({"sub": str(user.id)})
    
    return {
        "success": True, 
        "access_token": access_token, 
        "refresh_token": refresh_token,
        "user": {"id": user.id, "email": user.email}
    }


@router.post("/api/auth/refresh", response_model=TokenRefreshResponse)
async def refresh_token(payload: RefreshTokenSchema):
    try:
        decoded = jwt.decode(payload.refresh_token, JWT_SECRET_KEY, algorithms=[JWT_ALGORITHM])
        if decoded.get("type") != "refresh":
            raise HTTPException(status_code=401, detail="Невалідний тип токена. Очікується refresh-токен.")
            
        user_id = decoded.get("sub")
        jti = decoded.get("jti")
        exp = decoded.get("exp")
        
        if not user_id or not jti:
            raise HTTPException(status_code=401, detail="Невалідний payload refresh-токена.")

        redis = await get_redis()
        if redis:
            is_revoked = await redis.get(f"token:revoked:{jti}")
            if is_revoked:
                raise HTTPException(status_code=401, detail="Спроба повторного використання токена. Увійдіть знову.")

            ttl = int(exp - time.time())
            if ttl > 0:
                await redis.set(f"token:revoked:{jti}", "reused", ex=ttl)

        new_access_token = create_access_token({"sub": str(user_id)})
        new_refresh_token = create_refresh_token({"sub": str(user_id)})
        
        return {
            "success": True, 
            "access_token": new_access_token,
            "refresh_token": new_refresh_token
        }
        
    except jwt.ExpiredSignatureError:
        raise HTTPException(status_code=401, detail="Термін дії refresh-токена закінчився.")
    except (jwt.PyJWTError, ValueError, TypeError):
        raise HTTPException(status_code=401, detail="Помилка валідації refresh-токена.")


@router.post("/api/auth/logout", response_model=StandardStatusResponse)
async def logout(payload: Optional[LogoutSchema] = None, credentials: HTTPAuthorizationCredentials = Depends(security_scheme)):
    redis = await get_redis()
    if credentials:
        try:
            token_payload = jwt.decode(credentials.credentials, JWT_SECRET_KEY, algorithms=[JWT_ALGORITHM], options={"verify_signature": True})
            jti = token_payload.get("jti")
            exp = token_payload.get("exp")
            if jti and exp and redis:
                ttl = int(exp - time.time())
                if ttl > 0:
                    await redis.set(f"token:revoked:{jti}", "revoked", ex=ttl)
        except Exception:
            pass

    if payload and payload.refresh_token:
        try:
            ref_payload = jwt.decode(payload.refresh_token, JWT_SECRET_KEY, algorithms=[JWT_ALGORITHM], options={"verify_signature": True})
            ref_jti = ref_payload.get("jti")
            ref_exp = ref_payload.get("exp")
            if ref_jti and ref_exp and redis:
                ref_ttl = int(ref_exp - time.time())
                if ref_ttl > 0:
                    await redis.set(f"token:revoked:{ref_jti}", "revoked", ex=ref_ttl)
        except Exception:
            pass

    return {"success": True, "message": "Успішний вихід із системи."}