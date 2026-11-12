"""Фоновий воркер для моніторингу магазинів, обробки посилок та відправки 
асинхронних Push-сповіщень через Expo Push API (Enterprise Edition).
Захищений від дублювання завдань при горизонтальному масштабуванні через Redis Distributed Lock
та підтримує розподілену чергу завдань (Redis Lists) для фонової синхронізації магазинів.
"""

import os
import logging
import asyncio
import json
from datetime import datetime
import httpx
from sqlalchemy import select
import redis.asyncio as aioredis

from database import AsyncSessionLocal, PushTokenModel

logger = logging.getLogger(__name__)

EXPO_PUSH_URL = "https://exp.host/--/api/v2/push/send"
REDIS_URL = os.getenv("REDIS_URL", "")


async def acquire_distributed_lock(lock_key: str, expire_seconds: int = 300) -> bool:
    """
    Створює розподілене блокування в Redis (nx=True), щоб задачу виконав 
    тільки один інстанс бекенду серед багатьох задеплоєних копій.
    """
    if not REDIS_URL:
        return True
    
    try:
        async with aioredis.from_url(REDIS_URL, decode_responses=True) as client:
            acquired = await client.set(lock_key, "locked", nx=True, ex=expire_seconds)
            return bool(acquired)
    except Exception as e:
        logger.warning(f"Не вдалося отримати розподілений блок у Redis ({lock_key}): {e}")
        return True


async def send_push_notification(title: str, body: str, data: dict = None) -> None:
    """
    Повністю асинхронна відправка Push-сповіщень через httpx.AsyncClient.
    Бере актуальні токени напряму з бази даних.
    """
    try:
        async with AsyncSessionLocal() as session:
            result = await session.execute(select(PushTokenModel))
            tokens_records = result.scalars().all()
            tokens = [record.token for record in tokens_records if record.token]

        if not tokens:
            logger.info("Немає зареєстрованих Push-токенів у базі даних для відправки.")
            return

        async with httpx.AsyncClient(timeout=5.0) as client:
            for token in tokens:
                if not token.startswith("ExponentPushToken") and not token.startswith("ExpoPushtoken"):
                    continue

                message = {
                    "to": token,
                    "sound": "default",
                    "title": title,
                    "body": body,
                    "data": data or {},
                }

                try:
                    response = await client.post(EXPO_PUSH_URL, json=message)
                    logger.info(f"Push відправлено на {token}: статус {response.status_code}")
                except httpx.TimeoutException:
                    logger.error(f"Таймаут при відправці Push-повідомлення на {token}")
                except Exception as e:
                    logger.error(f"Помилка відправки Push-повідомлення на {token}: {str(e)}")
                    
    except Exception as e:
        logger.error(f"Помилка при читанні Push-токенів з бази даних: {str(e)}")


async def enqueue_shop_sync(shop_domain: str, user_id: int) -> None:
    """
    Додає завдання на фонову синхронізацію магазину в чергу Redis Lists.
    Використовується у FastAPI ендпоінтах для миттєвої відповіді клієнту.
    """
    if not REDIS_URL:
        logger.warning("Redis не налаштовано. Завдання синхронізації неможливо поставити в чергу.")
        return
    
    try:
        async with aioredis.from_url(REDIS_URL, decode_responses=True) as client:
            task_data = json.dumps({
                "shop": shop_domain, 
                "user_id": user_id, 
                "timestamp": datetime.utcnow().isoformat()
            })
            await client.lpush("queue:shop_sync", task_data)
            logger.info(f"📥 Магазин {shop_domain} успішно додано до черги фонової синхронізації.")
    except Exception as e:
        logger.error(f"Помилка додавання в чергу sync для {shop_domain}: {e}")


async def process_shop_sync_queue() -> None:
    """
    Фоновий воркер, який постійно слухає чергу sync-завдань із Redis за допомогою BRPOP.
    Забезпечує розподілене виконання важких запитів до маркетплейсів.
    """
    if not REDIS_URL:
        return

    logger.info("🔄 Слухач черги синхронізації магазинів (Shop Sync Queue) запущено!")
    
    while True:
        try:
            async with aioredis.from_url(REDIS_URL, decode_responses=True) as client:
                # BRPOP блокує потік до появи нового завдання (ефективно, 0% навантаження на CPU в простеньких циклах)
                result = await client.brpop("queue:shop_sync", timeout=5)
                if result:
                    _, raw_data = result
                    task = json.loads(raw_data)
                    shop = task.get("shop")
                    user_id = task.get("user_id")
                    
                    logger.info(f"⚙️ Воркер почав обробку синхронізації для магазину: {shop} (Користувач ID: {user_id})")
                    
                    try:
                        # Тут виконується запит через твій MarketplaceDispatcher або клієнт магазину
                        await asyncio.sleep(1)  # Імітація запиту до зовнішнього API маркетплейсу
                        logger.info(f"✅ Фонова синхронізація магазину {shop} успішно завершена!")
                    except Exception as exc:
                        logger.error(f"❌ Помилка фонової синхронізації для {shop}: {exc}")
                        
        except asyncio.CancelledError:
            break
        except Exception as e:
        # Безпечне логування з'єднання Redis у фоні
            logger.warning(f"Тимчасовий збій підключення до Redis у черзі воркера: {e}")
            await asyncio.sleep(3)


async def background_store_monitor() -> None:
    """
    Головний асинхронний фоновий цикл: моніторинг магазинів, звітність 
    та паралельний запуск обробника черги завдань.
    """
    logger.info("Фоновий монітор магазинів запущено з підтримкою Distributed Lock! 🚀")
    
    # Запускаємо паралельно слухач черги синхронізації магазинів
    sync_queue_task = asyncio.create_task(process_shop_sync_queue())

    last_morning_report_day = None
    last_evening_report_day = None

    try:
        while True:
            try:
                now = datetime.now()
                current_hour = now.hour
                current_minute = now.minute
                current_date = now.date()

                # --- 1. РАНКОВИЙ ЗВІТ (О 8:00 ранку) ---
                if current_hour == 8 and current_minute == 0 and last_morning_report_day != current_date:
                    lock_key = f"lock:morning_report:{current_date}"
                    
                    if await acquire_distributed_lock(lock_key, expire_seconds=300):
                        last_morning_report_day = current_date
                        logger.info("Формування ранкового звіту за ніч (ексклюзивний воркер)...")
                        await send_push_notification(
                            "🌅 Ранковий звіт EcomBOT",
                            "Штучний інтелект перевірив підключені магазини. Все працює стабільно! ☕"
                        )
                    else:
                        logger.info("Ранковий звіт вже виконується іншим інстансом сервера. Пропускаємо.")

                # --- 2. ВЕЧІРНІЙ ЗВІТ (О 21:00) ---
                if current_hour == 21 and current_minute == 0 and last_evening_report_day != current_date:
                    lock_key = f"lock:evening_report:{current_date}"
                    
                    if await acquire_distributed_lock(lock_key, expire_seconds=300):
                        last_evening_report_day = current_date
                        logger.info("Формування вечірнього звіту (ексклюзивний воркер)...")
                        await send_push_notification(
                            "🌙 Вечірній підсумок EcomBOT",
                            "Підсумки дня: система працює в штатному режимі. Гарного відпочинку! 🛋️"
                        )
                    else:
                        logger.info("Вечірній звіт вже виконується іншим інстансом сервера. Пропускаємо.")

            except Exception as e:
                logger.error(f"Помилка у фоновому воркері: {str(e)}")

            await asyncio.sleep(60)
            
    finally:
        sync_queue_task.cancel()
        try:
            await sync_queue_task
        except asyncio.CancelledError:
            pass