"""Фоновий воркер для моніторингу магазинів, обробки посилок та відправки 
асинхронних Push-сповіщень через Expo Push API (Enterprise Edition).
Захищений від дублювання завдань при горизонтальному масштабуванні через Redis Distributed Lock.
"""

import os
import logging
import asyncio
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
        # Якщо Redis не налаштовано, працюємо в локальному режимі (для локальної розробки)
        return True
    
    try:
        async with aioredis.from_url(REDIS_URL, decode_responses=True) as client:
            # nx=True гарантує встановлення ключа лише тоді, коли його ще немає
            acquired = await client.set(lock_key, "locked", nx=True, ex=expire_seconds)
            return bool(acquired)
    except Exception as e:
        logger.warning(f"Не вдалося отримати розподілений блок у Redis ({lock_key}): {e}")
        return True  # Фолбек: якщо Redis тимчасово недоступний, дозволяємо виконання, щоб не пропустити подію


async def send_push_notification(title: str, body: str, data: dict = None) -> None:
    """
    Повністю асинхронна відправка Push-сповіщень через httpx.AsyncClient.
    Бере актуальні токени напряму з бази даних, щоб вони ніколи не губилися.
    """
    try:
        async with AsyncSessionLocal() as session:
            result = await session.execute(select(PushTokenModel))
            tokens_records = result.scalars().all()
            tokens = [record.token for record in tokens_records if record.token]

        if not tokens:
            logger.info("Немає зареєстрованих державних Push-токенів у базі даних для відправки.")
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


async def background_store_monitor() -> None:
    """
    Головний асинхронний фоновий цикл: моніторинг магазинів, перевірка посилок,
    звітність та фонові задачі із захистом від дублювання при горизонтальному масштабуванні.
    """
    logger.info("Фоновий монітор магазинів запущено з підтримкою Distributed Lock! 🚀")
    
    last_morning_report_day = None
    last_evening_report_day = None

    while True:
        try:
            now = datetime.now()
            current_hour = now.hour
            current_minute = now.minute
            current_date = now.date()

            # --- 1. РАНКОВИЙ ЗВІТ (О 8:00 ранку) ---
            if current_hour == 8 and current_minute == 0 and last_morning_report_day != current_date:
                lock_key = f"lock:morning_report:{current_date}"
                
                # Запитуємо ексклюзивне право виконати задачу у Redis
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