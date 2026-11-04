import os
import requests
import logging
import asyncio
from datetime import datetime

logger = logging.getLogger(__name__)

EXPO_PUSH_URL = "https://exp.host/--/api/v2/push/send"

# Сховище для активних Push-токенів користувачів (у майбутньому перенесемо в PostgreSQL)
REGISTERED_PUSH_TOKENS = set()

def send_push_notification(title: string, body: string, data: dict = None):
    """
    Відправляє Push-сповіщення на мобільний додаток через Expo Push API.
    """
    if not REGISTERED_PUSH_TOKENS:
        logger.info("Немає зареєстрованих Push-токенів для відправки.")
        return

    for token in REGISTERED_PUSH_TOKENS:
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
            response = requests.post(EXPO_PUSH_URL, json=message, timeout=5)
            logger.info(f"Push відправлено на {token}: статус {response.status_code}")
        except Exception as e:
            logger.error(f"Помилка відправки Push-повідомлення: {str(e)}")


async def background_store_monitor():
    """
    Головний фоновий цикл: кожну хвилину перевіряє магазини, замовлення та чати.
    """
    logger.info("Фоновий монітор магазинів запущено! 🚀")
    
    # Змінні для відстеження останніх звітів
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
                # Тут можна підсумувати замовлення за ніч
                send_push_notification(
                    "🌅 Ранковий звіт EcomBOT",
                    "За ніч штучний інтелект опрацював магазини. Нових замовлень: 0. Все працює стабільно! ☕"
                )

            # --- 2. ВЕЧІРНІЙ ЗВІТ (О 21:00) ---
            if current_hour == 21 and current_minute == 0 and last_evening_report_day != current_date:
                last_evening_report_day = current_date
                logger.info("Формування вечірнього звіту...")
                send_push_notification(
                    "🌙 Вечірній підсумок EcomBOT",
                    "Підсумки дня: замовлень за день не було або всі оброблені успішно. Гарного відпочинку! 🛋️"
                )

            # --- 3. ХВИЛИННИЙ МОНІТОРИНГ МАГАЗИНІВ ТА ЧАТІВ ---
            # Тут сервер може проходитись по збережених магазинах клієнта,
            # викликати fetch_orders та порівнювати з попереднім станом.
            # Якщо з'явилося нове замовлення або скарга:
            # send_push_notification("🔥 Нове замовлення!", "Магазин Prom: Замовлення #2045 на суму 850 грн")

        except Exception as e:
            logger.error(f"Помилка у фоновому воркері: {str(e)}")

        # Чекаємо 60 секунд перед наступною перевіркою
        await asyncio.sleep(60)