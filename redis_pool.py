"""Єдиний спільний Redis-клієнт для всього застосунку (connection pool) з потокобезпечною ініціалізацією."""

import os
import logging
import asyncio
from typing import Optional
import redis.asyncio as aioredis

logger = logging.getLogger(__name__)

REDIS_URL = os.getenv("REDIS_URL", "")
_client: Optional[aioredis.Redis] = None
_init_lock = asyncio.Lock()


async def get_redis() -> Optional[aioredis.Redis]:
    """Повертає спільний клієнт з потокобезпечною лінивою ініціалізацією через asyncio.Lock. None — якщо Redis не налаштовано."""
    global _client
    if not REDIS_URL:
        return None
    
    if _client is None:
        async with _init_lock:
            # Подвійна перевірка (Double-checked locking) для запобігання зайвим ініціалізаціям
            if _client is None:
                try:
                    _client = aioredis.from_url(
                        REDIS_URL,
                        decode_responses=True,
                        socket_timeout=10.0,
                        socket_connect_timeout=10.0,
                        retry_on_timeout=True,
                        health_check_interval=30,
                        max_connections=20,
                    )
                    # Тестовий пінг для перевірки доступності
                    await _client.ping()
                    logger.info("Успішно ініціалізовано пул з'єднань Redis.")
                except Exception as e:
                    logger.error(f"Не вдалося підключитися до Redis під час ініціалізації пулу: {e}")
                    _client = None
    return _client


async def close_redis() -> None:
    """Закриває спільний пул з'єднань при виході із застосунку."""
    global _client
    if _client:
        try:
            await _client.aclose()
            logger.info("Пул з'єднань Redis успішно закрито.")
        except AttributeError:
            await _client.close()
            logger.info("Пул з'єднань Redis успішно закрито (legacy close).")
        except Exception as e:
            logger.error(f"Помилка при закритті з'єднань з Redis: {e}")
        finally:
            _client = None