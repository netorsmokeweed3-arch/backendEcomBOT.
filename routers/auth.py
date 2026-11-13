import os
import time
import uuid
import logging
import asyncio  
from datetime import datetime, timezone, timedelta
from typing import Optional

from fastapi import APIRouter, HTTPException, Request, Depends, status
from fastapi.security import HTTPAuthorizationCredentials
from pydantic import BaseModel, EmailStr, Field, field_validator
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

router = APIRouter(prefix="/api/auth", tags=["Auth"])

# --- PRINCIPAL PATTERN: Декларативний Rate Limiter ---
class RateLimitChecker:
    def __init__(self, prefix: str, max_requests: int, window_seconds: int = 60):
        self.prefix = prefix
        self.max_requests = max_requests
        self.window_seconds = window_seconds

    async def __call__(self, request: Request) -> None:
        client_ip = request.client.host if request.client else "127.0.0.1"
        if not await check_rate_limit(f"{self.prefix}:{client_ip}", max_requests=self.max_requests, window_seconds=self.window_seconds):
            logger.warning(f"Перевищено ліміт ({self.prefix}) для IP: {client_ip}")
            raise HTTPException(status_code=status.HTTP_429_TOO_MANY_REQUESTS, detail="Забагато спроб. Зачекайте.")


# --- СХЕМИ Pydantic ---
class UserRegisterSchema(BaseModel):
    email: EmailStr
    password: str = Field(..., min_length=8)

    @field_validator("password")
    @classmethod
    def validate_password(cls, v: str) -> str:
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


# --- ВИХІДНІ СХЕМИ ---
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
    message: str


# --- БЕЗПЕЧНІ КРИПТОГРАФІЧНІ УТИЛІТИ ---
def _hash_pwd(p: str) -> str:
    return bcrypt.hashpw(p.encode('utf-8'), bcrypt.gensalt()).decode('utf-8')

def _verify_pwd(plain: str, hashed: str) -> bool:
    return bcrypt.checkpw(plain.encode('utf-8'), hashed.encode('utf-8'))

async def hash_password(password: str) -> str:
    return await asyncio.to_thread(_hash_pwd, password)

async def verify_password(plain: str, hashed: str) -> bool:
    return await asyncio.to_thread(_verify_pwd, plain, hashed)

def create_token(sub: str, token_type: str, expires_delta: timedelta) -> str:
    expire = datetime.now(timezone.utc) + expires_delta
    payload = {"sub": sub, "type": token_type, "exp": expire, "jti": str(uuid.uuid4())}
    return jwt.encode(payload, JWT_SECRET_KEY, algorithm=JWT_ALGORITHM)


# --- ПРИВАТНИЙ HELPER ДЛЯ БЛЕКЛИСТУ (REDIS REVOCATION) ---
async def _revoke_token_in_redis(token: str, expected_type: str, reason: str) -> None:
    """Універсальний метод ротації та відкликання токенів у Redis (DRY)."""
    try:
        payload = jwt.decode(token, JWT_SECRET_KEY, algorithms=[JWT_ALGORITHM], options={"verify_signature": True})
        if payload.get("type") != expected_type:
            return
        jti, exp = payload.get("jti"), payload.get("exp")
        if jti and exp:
            redis = await get_redis()
            if redis:
                ttl = int(exp - time.time())
                if ttl > 0:
                    await redis.set(f"token:revoked:{jti}", reason, ex=ttl)
    except Exception as e:
        logger.debug(f"Не вдалося відкликати токен ({expected_type}): {e}")


@router.post("/register", response_model=AuthResponse, status_code=status.HTTP_201_CREATED, dependencies=[Depends(RateLimitChecker("reg", 5))])
async def register(payload: UserRegisterSchema, db: AsyncSession = Depends(get_db)):
    if await db.scalar(select(UserModel).where(UserModel.email == payload.email)):
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Користувач із такою електронною поштою вже існує.")
    
    new_user = UserModel(email=payload.email, hashed_password=await hash_password(payload.password))
    db.add(new_user)
    try:
        await db.commit()
    except IntegrityError:
        await db.rollback()
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Користувач із такою електронною поштою вже існує.")
    
    await db.refresh(new_user)
    sub_str = str(new_user.id)
    
    return {
        "success": True, 
        "access_token": create_token(sub_str, "access", timedelta(minutes=JWT_ACCESS_EXPIRATION_MINUTES)), 
        "refresh_token": create_token(sub_str, "refresh", timedelta(days=JWT_REFRESH_EXPIRATION_DAYS)),
        "user": {"id": new_user.id, "email": new_user.email}
    }


@router.post("/login", response_model=AuthResponse, status_code=status.HTTP_200_OK, dependencies=[Depends(RateLimitChecker("login", 10))])
async def login(payload: UserLoginSchema, db: AsyncSession = Depends(get_db)):
    user = await db.scalar(select(UserModel).where(UserModel.email == payload.email))
    if not user or not await verify_password(payload.password, user.hashed_password):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Невірний email або пароль.")
    
    sub_str = str(user.id)
    return {
        "success": True, 
        "access_token": create_token(sub_str, "access", timedelta(minutes=JWT_ACCESS_EXPIRATION_MINUTES)), 
        "refresh_token": create_token(sub_str, "refresh", timedelta(days=JWT_REFRESH_EXPIRATION_DAYS)),
        "user": {"id": user.id, "email": user.email}
    }


@router.post("/refresh", response_model=TokenRefreshResponse, status_code=status.HTTP_200_OK)
async def refresh_token(payload: RefreshTokenSchema):
    try:
        decoded = jwt.decode(payload.refresh_token, JWT_SECRET_KEY, algorithms=[JWT_ALGORITHM])
        if decoded.get("type") != "refresh":
            raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Невалідний тип токена.")
            
        user_id, jti = decoded.get("sub"), decoded.get("jti")
        if not user_id or not jti:
            raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Невалідний payload.")

        redis = await get_redis()
        if redis and await redis.get(f"token:revoked:{jti}"):
            logger.critical(f"КРИТИЧНО: Спроба повторного використання refresh-токена для user_id={user_id}!")
            raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Спроба повторного використання токена. Увійдіть знову.")

        # Відкликаємо старий токен (Reuse Detection)
        await _revoke_token_in_redis(payload.refresh_token, "refresh", "reused")

        sub_str = str(user_id)
        return {
            "success": True, 
            "access_token": create_token(sub_str, "access", timedelta(minutes=JWT_ACCESS_EXPIRATION_MINUTES)),
            "refresh_token": create_token(sub_str, "refresh", timedelta(days=JWT_REFRESH_EXPIRATION_DAYS))
        }
        
    except jwt.ExpiredSignatureError:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Термін дії refresh-токена закінчився.")
    except (jwt.PyJWTError, ValueError, TypeError):
        raise HTTPException(status_code=401, detail="Помилка валідації токена.")


@router.post("/logout", response_model=StandardStatusResponse, status_code=status.HTTP_200_OK)
async def logout(
    payload: Optional[LogoutSchema] = None, 
    credentials: Optional[HTTPAuthorizationCredentials] = Depends(security_scheme)
):
    if credentials:
        await _revoke_token_in_redis(credentials.credentials, "access", "revoked")
    if payload and payload.refresh_token:
        await _revoke_token_in_redis(payload.refresh_token, "refresh", "revoked")

    return {"success": True, "message": "Успішний вихід із системи. Сесію завершено."}