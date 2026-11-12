"""Модуль управління чергою фонових завдань через Redis (Redis Lists).
Відповідає за постановку задач у чергу (enqueue) через спільний Redis-пул.
"""

import json
import logging
from datetime import datetime, timezone
from typing import Optional, Dict, Any
from redis_pool import get_redis

logger = logging.getLogger(__name__)
QUEUE_NAME = "queue:shop_sync"


async def enqueue_shop_sync(shop_domain: str, user_id: int) -> bool:
    """
    Додає завдання на фонову синхронізацію магазину в кінець черги Redis (RPUSH).
    Використовує єдиний спільний пул з'єднань.
    """
    client = await get_redis()
    if not client:
        logger.warning("Redis не налаштовано. Неможливо поставити задачу в чергу.")
        return False
    
    try:
        task_data = json.dumps({
            "shop": shop_domain,
            "user_id": user_id,
            "timestamp": datetime.now(timezone.utc).isoformat()
        }, ensure_ascii=False)
        
        await client.rpush(QUEUE_NAME, task_data)
        logger.info(f"📥 [Queue] Магазин {shop_domain} (Користувач ID: {user_id}) успішно додано в чергу синхронізації.")
        return True
    except Exception as e:
        logger.error(f"❌ [Queue] Помилка додавання в чергу для {shop_domain}: {e}")
        return False


async def dequeue_shop_sync(timeout: int = 5) -> Optional[Dict[str, Any]]:
    """
    Отримує завдання з черги за допомогою блокуючого витягування (BRPOP).
    """
    client = await get_redis()
    if not client:
        return None
    
    try:
        result = await client.brpop(QUEUE_NAME, timeout=timeout)
        if result:
            _, raw_data = result
            return json.loads(raw_data)
        return None
    except Exception as e:
        logger.warning(f"⚠️ [Queue] Тимчасовий збій при читанні черги з Redis: {e}")
        return None