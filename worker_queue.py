"""Модуль управління чергою фонових завдань через Redis (Reliable Queue з патерном ACK).
Забезпечує гарантію доставки та виконання завдань навіть при збоях воркера.
"""

import json
import logging
from datetime import datetime, timezone
from typing import Optional, Dict, Any
from redis_pool import get_redis

logger = logging.getLogger(__name__)
QUEUE_NAME = "queue:shop_sync"
PROCESSING_QUEUE_NAME = "queue:shop_sync:processing"


async def enqueue_shop_sync(shop_domain: str, user_id: int) -> bool:
    """
    Додає завдання на фонову синхронізацію магазину в кінець черги Redis (RPUSH).
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
    Отримує завдання з черги за допомогою атомарного перенесення (BRPOPLPUSH)
    у чергу обробки (processing), що гарантує збереження задачі при падінні воркера.
    """
    client = await get_redis()
    if not client:
        return None
    
    try:
        # Атомарно витягуємо з основної черги і кидаємо в processing
        raw_data = await client.brpoplpush(QUEUE_NAME, PROCESSING_QUEUE_NAME, timeout=timeout)
        if raw_data:
            # Зберігаємо сирий рядок усередині словника для подальшого ACK
            task = json.loads(raw_data)
            task["_raw_payload"] = raw_data
            return task
        return None
    except Exception as e:
        logger.warning(f"⚠️ [Queue] Тимчасовий збій при читанні черги з Redis: {e}")
        return None


async def ack_shop_sync(task: Dict[str, Any]) -> bool:
    """
    Підтвердження успішного виконання завдання (ACK) — видаляє його
    з черги обробки (processing).
    """
    client = await get_redis()
    if not client or "_raw_payload" not in task:
        return False
    
    try:
        raw_data = task["_raw_payload"]
        # Видаляємо рівно 1 входження з processing queue
        await client.lrem(PROCESSING_QUEUE_NAME, 1, raw_data)
        logger.info(f"✅ [Queue] Завдання для магазину {task.get('shop')} успішно підтверджено (ACK).")
        return True
    except Exception as e:
        logger.error(f"❌ [Queue] Помилка підтвердження (ACK) для завдання: {e}")
        return False
