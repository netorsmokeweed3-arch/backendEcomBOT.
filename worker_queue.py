"""Модуль управління чергою фонових завдань через Redis (Redis Lists).
Відповідає за постановку задач у чергу (enqueue) та їх отримання воркером (dequeue).
"""

import os
import json
import logging
import redis.asyncio as aioredis
from datetime import datetime
from typing import Optional, Dict, Any

logger = logging.getLogger(__name__)

REDIS_URL = os.getenv("REDIS_URL", "")
QUEUE_NAME = "queue:shop_sync"


async def get_redis_queue_client() -> Optional[aioredis.Redis]:
    """Створює підключення до Redis для черги завдань."""
    if not REDIS_URL:
        return None
    try:
        return aioredis.from_url(REDIS_URL, decode_responses=True, encoding="utf-8")
    except Exception as e:
        logger.error(f"Помилка підключення до Redis у worker_queue: {e}")
        return None


async def enqueue_shop_sync(shop_domain: str, user_id: int) -> bool:
    """
    Додає завдання на фонову синхронізацію магазину в кінець черги Redis (RPUSH).
    Викликається з ендпоінтів FastAPI для миттєвої відповіді клієнту без блокування.
    """
    client = await get_redis_queue_client()
    if not client:
        logger.warning("Redis не налаштовано. Неможливо поставити задачу в чергу.")
        return False
    
    try:
        task_data = json.dumps({
            "shop": shop_domain,
            "user_id": user_id,
            "timestamp": datetime.utcnow().isoformat()
        }, ensure_ascii=False)
        
        # Додаємо в чергу
        await client.rpush(QUEUE_NAME, task_data)
        logger.info(f"📥 [Queue] Магазин {shop_domain} (Користувач ID: {user_id}) успішно додано в чергу синхронізації.")
        return True
    except Exception as e:
        logger.error(f"❌ [Queue] Помилка додавання в чергу для {shop_domain}: {e}")
        return False
    finally:
        await client.close()


async def dequeue_shop_sync(timeout: int = 5) -> Optional[Dict[str, Any]]:
    """
    Отримує завдання з черги за допомогою блокуючого витягування (BRPOP).
    Використовується фоновим воркером (`background_worker.py`) для обробки.
    """
    client = await get_redis_queue_client()
    if not client:
        return None
    
    try:
        # BRPOP блокує з'єднання на час timeout, економлячи ресурси CPU
        result = await client.brpop(QUEUE_NAME, timeout=timeout)
        if result:
            _, raw_data = result
            return json.loads(raw_data)
        return None
    except Exception as e:
        logger.warning(f"⚠️ [Queue] Тимчасовий збій при читанні черги з Redis: {e}")
        return None
    finally:
        await client.close()