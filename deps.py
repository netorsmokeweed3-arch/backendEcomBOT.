"""Спільні залежності (Dependencies) та утиліти FastAPI."""

import logging
from typing import Optional
from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from redis.asyncio import Redis
import jwt

from config import JWT_SECRET_KEY, JWT_ALGORITHM

logger = logging.getLogger(__name__)

security_scheme = HTTPBearer(auto_error=False)

# Глобальне посилання на клієнт Redis, яке оновлюється з lifespan сервера
_redis_client: Optional[Redis] = None


def set_redis_client(client: Optional[Redis]) -> None:
    """Встановлює глобальний клієнт Redis при старті застосунку."""
    global _redis_client
    _redis_client = client


async def get_redis() -> Optional[Redis]:
    """Повертає активний інстанс Redis."""
    return _redis_client


async def check_rate_limit(identifier: str, max_requests: int, window_seconds: int = 60) -> bool:
    """Глобальна перевірка лімітів через Redis із безпечним фолбеком при збоях."""
    if not _redis_client:
        return True
    try:
        current = await _redis_client.incr(identifier)
        if current == 1:
            await _redis_client.expire(identifier, window_seconds)
        return current <= max_requests
    except Exception as e:
        logger.error(f"Помилка Rate Limiter у Redis: {e}")
        return True


async def get_current_user_id(credentials: Optional[HTTPAuthorizationCredentials] = Depends(security_scheme)) -> int:
    """Витягує та валідує user_id з валідного Access Token з перевіркою чорного списку (Revocation)."""
    if not credentials:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Відсутній токен авторизації.")
    try:
        payload = jwt.decode(credentials.credentials, JWT_SECRET_KEY, algorithms=[JWT_ALGORITHM])
        if payload.get("type") != "access":
            raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Невалідний тип токена.")
        
        user_id = payload.get("sub")
        jti = payload.get("jti")
        
        if not user_id:
            raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Невірний payload токена.")
        
        if _redis_client and jti and await _redis_client.get(f"token:revoked:{jti}"):
            raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Токен відкликано.")
            
        return int(user_id)
        
    except jwt.ExpiredSignatureError:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Термін дії токена закінчився.")
    except (jwt.PyJWTError, ValueError):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Помилка валідації токена.")