"""Єдиний спільний Redis-клієнт для всього застосунку (connection pool)."""

import os
import logging
from typing import Optional
import redis.asyncio as aioredis

logger = logging.getLogger(__name__)

REDIS_URL = os.getenv("REDIS_URL", "")
_client: Optional[aioredis.Redis] = None


async def get_redis() -> Optional[aioredis.Redis]:
    """Повертає спільний клієнт (лінива ініціалізація). None — якщо Redis не налаштовано."""
    global _client
    if not REDIS_URL:
        return None
    if _client is None:
        _client = aioredis.from_url(
            REDIS_URL,
            decode_responses=True,
            socket_timeout=10.0,
            socket_connect_timeout=10.0,
            retry_on_timeout=True,
            health_check_interval=30,
            max_connections=20,
        )
    return _client


async def close_redis() -> None:
    """Закриває спільний пул з'єднань при виході із застосунку."""
    global _client
    if _client:
        await _client.close()
        _client = None