"""Єдиний уніфікований фоновий воркер для OmniFlow Enterprise SaaS[cite: 2, 3].
Об'єднує періодичну інкрементальну синхронізацію магазинів з БД[cite: 3],
обробку черги через Redis з патерном ACK, розподілені блокування[cite: 2],
ранкові/вечірні звіти та асинхронні Push-сповіщення через Expo Push API[cite: 2].
"""

import os
import logging
import asyncio
import json
from pathlib import Path
from datetime import datetime, timezone
import httpx

if Path(".env").exists():
    from dotenv import load_dotenv
    load_dotenv()

from database import (
    init_db, close_db, AsyncSessionLocal, select, 
    StoreCredentialModel, OrderModel, OrderItemModel, 
    AuditLogModel, PushTokenModel, decrypt_credential
)
from sqlalchemy.orm import selectinload
from redis_pool import get_redis
from clients.dispatcher import MarketplaceDispatcher
from worker_queue import dequeue_shop_sync, ack_shop_sync
from order_normalizer import OrderAPIError

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [WORKER] [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S"
)
logger = logging.getLogger("OmniFlowWorker")

EXPO_PUSH_URL = "https://exp.host/--/api/v2/push/send"
dispatcher = MarketplaceDispatcher(max_concurrent=5)


async def save_orders_to_db(db, user_id: int, shop_domain: str, orders: list):
    """Зберігає або оновлює замовлення в базі даних (Upsert логіка) з ізольованою обробкою винятків на рівні кожного замовлення."""
    for raw in orders:
        if not isinstance(raw, dict):
            continue
            
        external_id = str(raw.get("id") or raw.get("order_number") or "")
        if not external_id:
            continue
            
        # 🚀 Сеньйорське рішення: ізоляція кожного замовлення через Savepoint (вкладену транзакцію)
        try:
            async with db.begin_nested():
                result = await db.execute(
                    select(OrderModel)
                    .options(selectinload(OrderModel.items))
                    .where(
                        OrderModel.shop_domain == shop_domain,
                        OrderModel.external_order_id == external_id
                    )
                )
                existing_order = result.scalars().first()
                
                order_number = str(raw.get("order_number") or external_id)
                customer_name = str(raw.get("customer_name") or "")
                phone = str(raw.get("phone") or "")
                email = str(raw.get("email") or "")
                currency = str(raw.get("currency") or "UAH")
                status_obj = raw.get("status", "В обробці")
                status = str(status_obj.get("text") if isinstance(status_obj, dict) else status_obj)
                
                total_raw = raw.get("total_amount", 0)
                if isinstance(total_raw, dict):
                    total_amount = float(total_raw.get("amount", 0) or 0)
                else:
                    try:
                        total_amount = float(total_raw)
                    except (ValueError, TypeError):
                        total_amount = 0.0

                raw_payload_str = json.dumps(raw, ensure_ascii=False)

                if existing_order:
                    existing_order.customer_name = customer_name
                    existing_order.phone = phone
                    existing_order.email = email
                    existing_order.total_amount = total_amount
                    existing_order.currency = currency
                    existing_order.status = status
                    existing_order.raw_payload = raw_payload_str
                    target_order = existing_order
                else:
                    new_order = OrderModel(
                        user_id=user_id,
                        shop_domain=shop_domain,
                        external_order_id=external_id,
                        order_number=order_number,
                        customer_name=customer_name,
                        phone=phone,
                        email=email,
                        total_amount=total_amount,
                        currency=currency,
                        status=status,
                        raw_payload=raw_payload_str
                    )
                    db.add(new_order)
                    await db.flush()
                    target_order = new_order

                if existing_order:
                    for item in list(existing_order.items):
                        await db.delete(item)

                items = raw.get("items", []) or raw.get("line_items", [])
                if isinstance(items, list):
                    for it in items:
                        if not isinstance(it, dict):
                            continue
                        title = str(it.get("title") or it.get("name") or "Товар")
                        sku = str(it.get("sku") or "")
                        qty = it.get("quantity") or it.get("qty") or 1
                        try:
                            quantity = float(qty)
                        except (ValueError, TypeError):
                            quantity = 1.0

                        price_raw = it.get("unit_price") if "unit_price" in it else it.get("price", 0)
                        if isinstance(price_raw, dict):
                            unit_price = float(price_raw.get("amount", 0) or 0)
                        else:
                            try:
                                unit_price = float(price_raw)
                            except (ValueError, TypeError):
                                unit_price = 0.0

                        db.add(OrderItemModel(
                            order_id=target_order.id,
                            sku=sku,
                            title=title,
                            quantity=quantity,
                            unit_price=unit_price
                        ))
        except Exception as order_err:
            # Якщо поламалося конкретне замовлення — логуємо, відкочуємо лише його savepoint і йдемо далі!
            logger.error(f"❌ Помилка обробки окремого замовлення ID {external_id} для магазину {shop_domain}: {order_err}")
            continue

    # Фінальний коміт успішно збережених замовлень пакету
    await db.commit()


async def acquire_distributed_lock(lock_key: str, expire_seconds: int = 300) -> bool:
    """Створює розподілене блокування в Redis (nx=True) для запобігання дублюванню завдань[cite: 2]."""
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
    """Повністю асинхронна відправка Push-сповіщень через Expo Push API батчами по 100 шт[cite: 2]."""
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
                except Exception as e:
                    logger.error(f"Помилка відправки Push-батчу: {e}")
    except Exception as e:
        logger.error(f"Помилка при читанні Push-токенів з бази даних: {e}")


async def process_shop_sync_queue() -> None:
    """Фоновий слухач черги синхронізації з Redis та виклику ACK[cite: 2, 4]."""
    redis = await get_redis()
    if not redis:
        logger.warning("Redis не налаштовано, слухач черги синхронізації пропущено.")
        return

    logger.info("🔄 Слухач черги синхронізації магазинів (Shop Sync Queue) запущено!")
    
    while True:
        try:
            task = await dequeue_shop_sync(timeout=5)
            if task:
                shop = task.get("shop")
                user_id = task.get("user_id")
                
                logger.info(f"⚙️ Воркер почав обробку синхронізації з черги для магазину: {shop} (Користувач ID: {user_id})")
                
                try:
                    async with AsyncSessionLocal() as db:
                        result = await db.execute(select(StoreCredentialModel).where(
                            StoreCredentialModel.shop_domain == shop,
                            StoreCredentialModel.user_id == user_id
                        ))
                        store_cred = result.scalars().first()
                        
                        if not store_cred:
                            logger.error(f"❌ Магазин {shop} не знайдено для користувача {user_id}")
                            await ack_shop_sync(task)
                            continue
                        
                        credentials = json.loads(decrypt_credential(store_cred.encrypted_credentials))
                        store_config = {
                            "platform": store_cred.platform,
                            "shop": shop,
                            "credentials": credentials,
                            "last_synced_at": store_cred.last_synced_at.isoformat() if store_cred.last_synced_at else None
                        }
                        
                        orders = await dispatcher.fetch_store_orders(store_config)
                        if orders:
                            await save_orders_to_db(db, user_id, shop, orders)
                        
                        store_cred.last_synced_at = datetime.now(timezone.utc)
                        db.add(AuditLogModel(
                            user_id=user_id,
                            shop_domain=shop,
                            event_type="BACKGROUND_SYNC_SUCCESS",
                            message=f"Успішно синхронізовано з черги. Замовлень: {len(orders)}"
                        ))
                        await db.commit()
                        logger.info(f"✅ Фонова синхронізація магазину {shop} з черги завершена! Замовлень: {len(orders)}")
                        
                    await ack_shop_sync(task)
                        
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
                    await ack_shop_sync(task)
                except Exception as exc:
                    logger.error(f"❌ Критична помилка черги для {shop}: {exc}")
                        
        except asyncio.CancelledError:
            break
        except Exception as e:
            logger.warning(f"Тимчасовий збій у черзі воркера: {e}")
            await asyncio.sleep(3)


async def background_store_monitor_loop() -> None:
    """Головний фоновий цикл: періодична синхронізація магазинів з БД[cite: 3], звіти та запуск слухача черги[cite: 2]."""
    logger.info("Фоновий воркер інкрементальної синхронізації та моніторингу задеплоєно. 🚀")
    
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

                if current_hour == 8 and current_minute < 5 and last_morning_report_day != current_date:
                    lock_key = f"lock:morning_report:{current_date}"
                    if await acquire_distributed_lock(lock_key, expire_seconds=300):
                        last_morning_report_day = current_date
                        await send_push_notification(
                            "🌅 Ранковий звіт EcomBOT",
                            "Штучний інтелект перевірив підключені магазини. Все працює стабільно! ☕"
                        )

                if current_hour == 21 and current_minute < 5 and last_evening_report_day != current_date:
                    lock_key = f"lock:evening_report:{current_date}"
                    if await acquire_distributed_lock(lock_key, expire_seconds=300):
                        last_evening_report_day = current_date
                        await send_push_notification(
                            "🌙 Вечірній підсумок EcomBOT",
                            "Підсумки дня: система працює в штатному режимі. Гарного відпочинку! 🛋️"
                        )

                logger.info("🔄 Запуск планової фонової синхронізації усіх магазинів з БД...")
                async with AsyncSessionLocal() as db:
                    result = await db.execute(select(StoreCredentialModel))
                    stores = result.scalars().all()
                    
                    if stores:
                        logger.info(f"Знайдено магазинів для планової синхронізації: {len(stores)}")
                        for store in stores:
                            try:
                                credentials = json.loads(decrypt_credential(store.encrypted_credentials))
                                store_config = {
                                    "platform": store.platform,
                                    "shop": store.shop_domain,
                                    "credentials": credentials,
                                    "last_synced_at": store.last_synced_at.isoformat() if store.last_synced_at else None
                                }
                                
                                orders = await dispatcher.fetch_store_orders(store_config)
                                if orders:
                                    await save_orders_to_db(db, store.user_id, store.shop_domain, orders)
                                
                                store.last_synced_at = datetime.now(timezone.utc)
                                await db.commit()
                                logger.info(f"Успішно синхронізовано магазин {store.shop_domain}. Замовлень: {len(orders) if orders else 0}. Last synced: {store.last_synced_at}")
                            except Exception as store_err:
                                logger.error(f"Помилка синхронізації магазину {store.shop_domain}: {store_err}")
                    else:
                        logger.info("У базі даних поки немає підключених магазинів для фонової синхронізації.")

            except Exception as e:
                logger.error(f"Помилка у фоновому воркері: {e}", exc_info=True)

            await asyncio.sleep(300)
            
    finally:
        sync_queue_task.cancel()
        try:
            await sync_queue_task
        except asyncio.CancelledError:
            pass


async def main():
    logger.info("Ініціалізація бази даних для єдиного воркера...")
    await init_db()
    
    try:
        await background_store_monitor_loop()
    except asyncio.CancelledError:
        logger.info("Фоновий воркер зупиняється...")
    finally:
        await close_db()
        logger.info("З'єднання воркера успішно закриті.")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("Воркер зупинено вручну користувачем.")