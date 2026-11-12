"""Фоновий воркер для моніторингу магазинів, обробки посилок та відправки 
асинхронних Push-сповіщень через Expo Push API (Enterprise Edition).
Захищений від дублювання завдань при горизонтальному масштабуванні через Redis Distributed Lock
та підтримує розподілену чергу завдань (Redis Lists) для фонової синхронізації магазинів.
"""

import os
import logging
import asyncio
from datetime import datetime, timezone
import httpx
from sqlalchemy import select

from database import AsyncSessionLocal, PushTokenModel, StoreCredentialModel, AuditLogModel
from redis_pool import get_redis
from clients.dispatcher import MarketplaceDispatcher
from worker_queue import dequeue_shop_sync
from order_normalizer import OrderAPIError

logger = logging.getLogger(__name__)

EXPO_PUSH_URL = "https://exp.host/--/api/v2/push/send"
dispatcher = MarketplaceDispatcher(max_concurrent=5)


async def acquire_distributed_lock(lock_key: str, expire_seconds: int = 300) -> bool:
    """
    Створює розподілене блокування в Redis (nx=True), щоб задачу виконав 
    тільки один інстанс бекенду серед багатьох задеплоєних копій.
    """
    redis = await get_redis()
    if not redis:
        return True
    
    try:
        acquired = await redis.set(lock_key, "locked", nx=True, ex=expire_seconds)
        return bool(acquired)
    except Exception as e:
        logger.warning(f"Не вдалося отримати розподілений блок у Redis ({lock_key}): {e}")
        return True


async def send_push_notification(title: str, body: str, data: dict = None, user_id: int = None) -> None:
    """
    Повністю асинхронна відправка Push-сповіщень через httpx.AsyncClient батчами по 100 шт.
    """
    try:
        async with AsyncSessionLocal() as session:
            query = select(PushTokenModel)
            if user_id is not None:
                query = query.where(PushTokenModel.user_id == user_id)
            result = await session.execute(query)
            tokens_records = result.scalars().all()
            tokens = [record.token for record in tokens_records if record.token]

        valid_tokens = [t for t in tokens if t.startswith("ExponentPushToken")]
        if not valid_tokens:
            logger.info("Немає зареєстрованих валідних Push-токенів для відправки.")
            return

        async with httpx.AsyncClient(timeout=10.0) as client:
            for i in range(0, len(valid_tokens), 100):
                batch = [
                    {
                        "to": token,
                        "sound": "default",
                        "title": title,
                        "body": body,
                        "data": data or {},
                    }
                    for token in valid_tokens[i:i + 100]
                ]

                try:
                    response = await client.post(EXPO_PUSH_URL, json=batch)
                    logger.info(f"Push-батч ({len(batch)} шт.) відправлено: статус {response.status_code}")
                except httpx.TimeoutException:
                    logger.error("Таймаут при відправці Push-батчу")
                except Exception as e:
                    logger.error(f"Помилка відправки Push-батчу: {str(e)}")
                    
    except Exception as e:
        logger.error(f"Помилка при читанні Push-токенів з бази даних: {str(e)}")


async def process_shop_sync_queue() -> None:
    """
    Фоновий воркер, який постійно слухає чергу sync-завдань із Redis за допомогою BRPOP
    та виконує реальну синхронізацію магазину.
    """
    redis = await get_redis()
    if not redis:
        return

    logger.info("🔄 Слухач черги синхронізації магазинів (Shop Sync Queue) запущено!")
    
    while True:
        try:
            task = await dequeue_shop_sync(timeout=5)
            if task:
                shop = task.get("shop")
                user_id = task.get("user_id")
                
                logger.info(f"⚙️ Воркер почав обробку синхронізації для магазину: {shop} (Користувач ID: {user_id})")
                
                try:
                    async with AsyncSessionLocal() as db:
                        result = await db.execute(select(StoreCredentialModel).where(
                            StoreCredentialModel.shop_domain == shop,
                            StoreCredentialModel.user_id == user_id
                        ))
                        store_cred = result.scalars().first()
                        
                        if not store_cred:
                            logger.error(f"❌ Магазин {shop} не знайдено для користувача {user_id}")
                            continue
                        
                        import json as jsonlib
                        from database import decrypt_credential
                        credentials = jsonlib.loads(decrypt_credential(store_cred.encrypted_credentials))
                        
                        store_config = {
                            "platform": store_cred.platform,
                            "shop": shop,
                            "credentials": credentials
                        }
                        
                        orders = await dispatcher.fetch_store_orders(store_config)
                        logger.info(f"✅ Фонова синхронізація магазину {shop} успішно завершена! Завантажено замовлень: {len(orders)}")
                        
                        # Логування успіху в аудит
                        db.add(AuditLogModel(
                            user_id=user_id,
                            shop_domain=shop,
                            event_type="BACKGROUND_SYNC_SUCCESS",
                            message=f"Успішно синхронізовано. Замовлень: {len(orders)}"
                        ))
                        await db.commit()
                        
                except OrderAPIError as oae:
                    logger.error(f"❌ API помилка синхронізації для {shop}: {oae}")
                    async with AsyncSessionLocal() as db:
                        db.add(AuditLogModel(
                            user_id=user_id,
                            shop_domain=shop,
                            event_type="BACKGROUND_SYNC_ERROR",
                            message=str(oae)
                        ))
                        await db.commit()
                except Exception as exc:
                    logger.error(f"❌ Критична помилка фонової синхронізації для {shop}: {exc}")
                    
        except asyncio.CancelledError:
            break
        except Exception as e:
            logger.warning(f"Тимчасовий збій у черзі воркера: {e}")
            await asyncio.sleep(3)


async def background_store_monitor() -> None:
    """
    Головний асинхронний фоновий цикл: моніторинг магазинів, звітність 
    та паралельний запуск обробника черги завдань.
    """
    logger.info("Фоновий монітор магазинів запущено з підтримкою Distributed Lock! 🚀")
    
    sync_queue_task = asyncio.create_task(process_shop_sync_queue())

    last_morning_report_day = None
    last_evening_report_day = None

    try:
        while True:
            try:
                now = datetime.now(timezone.utc)
                current_hour = now.hour
                current_minute = now.minute
                current_date = now.date()

                # --- 1. РАНКОВИЙ ЗВІТ (О 8:00 UTC) ---
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

                # --- 2. ВЕЧІРНІЙ ЗВІТ (О 21:00 UTC) ---
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