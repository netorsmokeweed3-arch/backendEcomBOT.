import os
import time
import uuid
import secrets
import asyncio
import logging
from datetime import datetime, timezone, timedelta
from typing import Optional

import httpx
from fastapi import APIRouter, HTTPException, Request, Depends, status
from fastapi.security import HTTPAuthorizationCredentials
from pydantic import BaseModel, EmailStr, Field, field_validator
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
import jwt
import bcrypt

from database import get_db, UserModel
from config import JWT_SECRET_KEY, JWT_ALGORITHM, JWT_ACCESS_EXPIRATION_MINUTES, JWT_REFRESH_EXPIRATION_DAYS
from deps import security_scheme, check_rate_limit, get_redis

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/auth", tags=["Auth"])

BREVO_API_KEY = os.getenv("BREVO_API_KEY", "").strip()
BREVO_SENDER_EMAIL = (os.getenv("BREVO_SENDER_EMAIL") or os.getenv("SMTP_FROM") or os.getenv("SMTP_USER") or "").strip()
BREVO_SENDER_NAME = os.getenv("BREVO_SENDER_NAME", "OmniFlow").strip()
CODE_TTL_SECONDS = 600

_memory_codes: dict[str, tuple[str, float]] = {}


class RateLimitChecker:
    def __init__(self, prefix: str, max_requests: int, window_seconds: int = 60):
        self.prefix = prefix
        self.max_requests = max_requests
        self.window_seconds = window_seconds

    async def __call__(self, request: Request) -> None:
        client_ip = request.client.host if request.client else "127.0.0.1"
        if not await check_rate_limit(f"{self.prefix}:{client_ip}", max_requests=self.max_requests, window_seconds=self.window_seconds):
            raise HTTPException(status_code=status.HTTP_429_TOO_MANY_REQUESTS, detail="Забагато спроб. Зачекайте.")


class SendCodeSchema(BaseModel):
    email: EmailStr


class VerifyCodeSchema(BaseModel):
    email: EmailStr
    code: str = Field(..., min_length=4, max_length=4)


class UserRegisterSchema(BaseModel):
    email: EmailStr
    password: str = Field(..., min_length=8)

    @field_validator("password")
    @classmethod
    def validate_password(cls, v: str) -> str:
        if len(v.encode("utf-8")) > 72:
            raise ValueError("Пароль занадто довгий.")
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
    message: str


def _hash_pwd(p: str) -> str:
    return bcrypt.hashpw(p.encode("utf-8"), bcrypt.gensalt()).decode("utf-8")


def _verify_pwd(plain: str, hashed: str) -> bool:
    return bcrypt.checkpw(plain.encode("utf-8"), hashed.encode("utf-8"))


async def hash_password(password: str) -> str:
    return await asyncio.to_thread(_hash_pwd, password)


async def verify_password(plain: str, hashed: str) -> bool:
    return await asyncio.to_thread(_verify_pwd, plain, hashed)


def create_token(sub: str, token_type: str, expires_delta: timedelta) -> str:
    expire = datetime.now(timezone.utc) + expires_delta
    payload = {"sub": sub, "type": token_type, "exp": expire, "jti": str(uuid.uuid4())}
    return jwt.encode(payload, JWT_SECRET_KEY, algorithm=JWT_ALGORITHM)


async def _send_code_via_brevo(to_email: str, code: str) -> None:
    if not BREVO_API_KEY or not BREVO_SENDER_EMAIL:
        raise RuntimeError("Не налаштовано BREVO_API_KEY або адресу відправника.")

    payload = {
        "sender": {"name": BREVO_SENDER_NAME, "email": BREVO_SENDER_EMAIL},
        "to": [{"email": to_email}],
        "subject": f"Ваш код входу в OmniFlow: {code}",
        "textContent": f"Ваш 4-значний код для входу в акаунт: {code}\nКод дійсний 10 хвилин.",
    }
    headers = {
        "api-key": BREVO_API_KEY,
        "Content-Type": "application/json",
        "Accept": "application/json",
    }

    async with httpx.AsyncClient(timeout=15.0) as client:
        resp = await client.post("https://api.brevo.com/v3/smtp/email", json=payload, headers=headers)
        if resp.status_code not in (200, 201, 202):
            raise RuntimeError(f"Brevo API помилка {resp.status_code}: {resp.text}")


async def _save_login_code(email: str, code: str) -> None:
    key = f"auth:otp:{email.lower().strip()}"
    redis = await get_redis()
    if redis:
        await redis.set(key, code, ex=CODE_TTL_SECONDS)
    _memory_codes[key] = (code, time.time() + CODE_TTL_SECONDS)


async def _pop_login_code(email: str) -> Optional[str]:
    key = f"auth:otp:{email.lower().strip()}"
    redis = await get_redis()
    if redis:
        val = await redis.get(key)
        if val:
            await redis.delete(key)
            return val.decode() if isinstance(val, bytes) else str(val)
    item = _memory_codes.pop(key, None)
    if item and item[1] > time.time():
        return item[0]
    return None


async def _revoke_token_in_redis(token: str, expected_type: str, reason: str) -> None:
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


@router.post("/send-code", response_model=StandardStatusResponse, status_code=status.HTTP_200_OK, dependencies=[Depends(RateLimitChecker("send_code", 5))])
async def send_code(payload: SendCodeSchema):
    clean_email = payload.email.lower().strip()
    code = f"{secrets.randbelow(10000):04d}"
    await _save_login_code(clean_email, code)
    try:
        await _send_code_via_brevo(clean_email, code)
    except Exception as e:
        logger.error(f"Помилка відправки коду на {clean_email}: {e}")
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="Не вдалося надіслати код на email.")
    return {"success": True, "message": "Код надіслано на пошту."}


@router.post("/verify-code", response_model=AuthResponse, status_code=status.HTTP_200_OK, dependencies=[Depends(RateLimitChecker("verify_code", 10))])
async def verify_code(payload: VerifyCodeSchema, db: AsyncSession = Depends(get_db)):
    clean_email = payload.email.lower().strip()
    saved_code = await _pop_login_code(clean_email)
    if not saved_code or saved_code != payload.code.strip():
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Невірний або прострочений код.")

    user = await db.scalar(select(UserModel).where(UserModel.email == clean_email))
    if not user:
        random_pwd = secrets.token_urlsafe(16)
        user = UserModel(email=clean_email, hashed_password=await hash_password(random_pwd))
        db.add(user)
        try:
            await db.commit()
            await db.refresh(user)
        except IntegrityError:
            await db.rollback()
            user = await db.scalar(select(UserModel).where(UserModel.email == clean_email))

    sub_str = str(user.id)
    return {
        "success": True,
        "access_token": create_token(sub_str, "access", timedelta(minutes=JWT_ACCESS_EXPIRATION_MINUTES)),
        "refresh_token": create_token(sub_str, "refresh", timedelta(days=JWT_REFRESH_EXPIRATION_DAYS)),
        "user": {"id": user.id, "email": user.email},
    }


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
        "user": {"id": new_user.id, "email": new_user.email},
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
        "user": {"id": user.id, "email": user.email},
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
            raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Спроба повторного використання токена.")
        await _revoke_token_in_redis(payload.refresh_token, "refresh", "reused")
        sub_str = str(user_id)
        return {
            "success": True,
            "access_token": create_token(sub_str, "access", timedelta(minutes=JWT_ACCESS_EXPIRATION_MINUTES)),
            "refresh_token": create_token(sub_str, "refresh", timedelta(days=JWT_REFRESH_EXPIRATION_DAYS)),
        }
    except jwt.ExpiredSignatureError:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Термін дії refresh-токена закінчився.")
    except (jwt.PyJWTError, ValueError, TypeError):
        raise HTTPException(status_code=401, detail="Помилка валідації токена.")


@router.post("/logout", response_model=StandardStatusResponse, status_code=status.HTTP_200_OK)
async def logout(
    payload: Optional[LogoutSchema] = None,
    credentials: Optional[HTTPAuthorizationCredentials] = Depends(security_scheme),
):
    if credentials:
        await _revoke_token_in_redis(credentials.credentials, "access", "revoked")
    if payload and payload.refresh_token:
        await _revoke_token_in_redis(payload.refresh_token, "refresh", "revoked")
    return {"success": True, "message": "Успішний вихід із системи. Сесію завершено."}