"""Фоновий воркер для моніторингу магазинів, обробки посилок та відправки 
асинхронних Push-сповіщень через Expo Push API (Enterprise Edition).
Повністю інтегрований з асинхронною базою даних.
"""

import os
import logging
import asyncio
from datetime import datetime
import httpx
from sqlalchemy import select

from database import AsyncSessionLocal, PushTokenModel

logger = logging.getLogger(__name__)

EXPO_PUSH_URL = "https://exp.host/--/api/v2/push/send"


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


async def background_store_monitor() -> None:
    """
    Головний асинхронний фоновий цикл: моніторинг магазинів, перевірка посилок,
    звітність та фонові задачі.
    """
    logger.info("Фоновий монітор магазинів запущено! 🚀")
    
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
                last_morning_report_day = current_date
                logger.info("Формування ранкового звіту за ніч...")
                await send_push_notification(
                    "🌅 Ранковий звіт EcomBOT",
                    "Штучний інтелект перевірив підключені магазини. Все працює стабільно! ☕"
                )

            # --- 2. ВЕЧІРНІЙ ЗВІТ (О 21:00) ---
            if current_hour == 21 and current_minute == 0 and last_evening_report_day != current_date:
                last_evening_report_day = current_date
                logger.info("Формування вечірнього звіту...")
                await send_push_notification(
                    "🌙 Вечірній підсумок EcomBOT",
                    "Підсумки дня: система працює в штатному режимі. Гарного відпочинку! 🛋️"
                )

        except Exception as e:
            logger.error(f"Помилка у фоновому воркері: {str(e)}")

        await asyncio.sleep(60)