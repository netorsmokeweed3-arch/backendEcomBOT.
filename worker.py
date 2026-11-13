"""Окремий незалежний фоновий воркер для OmniFlow Enterprise SaaS.
Працює у фоновому режимі, збирає замовлення з маркетплейсів,
зберігає їх у базу даних та оновлює мітку інкрементальної синхронізації (last_synced_at).
"""

import asyncio
import logging
import os
import json
from pathlib import Path
from datetime import datetime, timezone

if Path(".env").exists():
    from dotenv import load_dotenv
    load_dotenv()

from database import init_db, close_db, AsyncSessionLocal, select, StoreCredentialModel, OrderModel, OrderItemModel, decrypt_credential
from sqlalchemy.orm import selectinload
from clients.dispatcher import MarketplaceDispatcher

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [WORKER] [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S"
)
logger = logging.getLogger("OmniFlowWorker")

dispatcher = MarketplaceDispatcher(max_concurrent=5)


async def save_orders_to_db(db, user_id: int, shop_domain: str, orders: list):
    """Зберігає або оновлює замовлення в базі даних (Upsert логіка)."""
    for raw in orders:
        if not isinstance(raw, dict):
            continue
            
        external_id = str(raw.get("id") or raw.get("order_number") or "")
        if not external_id:
            continue
            
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

    await db.commit()


async def background_store_monitor_loop():
    """Фоновий цикл інкрементального моніторингу та синхронізації магазинів."""
    logger.info("Фоновий воркер інкрементальної синхронізації задеплоєно. 🔄")
    
    while True:
        try:
            logger.info("Запуск планової фонової синхронізації магазинів з БД...")
            async with AsyncSessionLocal() as db:
                result = await db.execute(select(StoreCredentialModel))
                stores = result.scalars().all()
                
                if stores:
                    logger.info(f"Знайдено магазинів для синхронізації: {len(stores)}")
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


async def main():
    logger.info("Ініціалізація бази даних для воркера...")
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