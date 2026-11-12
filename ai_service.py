import json
import logging
import asyncio
import hashlib
import os
from typing import Optional, Dict, Any
import redis.asyncio as aioredis
from agents import MultiAgentSystem, fetch_realtime_currency_rates

logger = logging.getLogger(__name__)

REDIS_URL = os.getenv("REDIS_URL", "")

async def get_redis_cache() -> Optional[aioredis.Redis]:
    """Надійне підключення до Redis для ідемпотентності та кешу з автоповтором та більшими таймаутами."""
    if not REDIS_URL:
        return None
    try:
        client = aioredis.from_url(
            REDIS_URL, 
            decode_responses=True,
            socket_timeout=10.0,          # Даємо хмарі більше часу на відповідь
            socket_connect_timeout=10.0,  # Таймаут на з'єднання
            retry_on_timeout=True,        # Автоматично повторювати спробу при мікро-таймаутах
            health_check_interval=20      # Регулярно перевіряти статус з'єднання
        )
        await client.ping()
        return client
    except Exception as e:
        logger.warning(f"Redis тимчасово недоступний: {e}")
        return None


def standard_fields(order: dict) -> dict:
    """Нормалізує базові поля замовлення у безпечний внутрішній формат."""
    safe_order = order if isinstance(order, dict) else {}
    address = safe_order.get("delivery_address") or safe_order.get("shipping_address") or {}
    total = safe_order.get("total_amount") or {}
    status_obj = safe_order.get("status") or {}

    if isinstance(address, str):
        full_address = address
        city, region, postal_code, country = "", "", "", ""
    elif isinstance(address, dict):
        full_address = str(address.get("full") or address.get("address1") or "")
        city = str(address.get("city", ""))
        region = str(address.get("region") or address.get("province") or address.get("state", ""))
        postal_code = str(address.get("postal_code") or address.get("zip", ""))
        country = str(address.get("country") or address.get("country_name", ""))
    else:
        full_address, city, region, postal_code, country = "", "", "", "", ""

    if isinstance(total, (int, float, str)):
        total_formatted = str(total)
    elif isinstance(total, dict):
        total_formatted = str(total.get("formatted") or total.get("amount", ""))
    else:
        total_formatted = ""

    currency = str(safe_order.get("currency") or (total.get("currency") if isinstance(total, dict) else "") or "")

    if isinstance(status_obj, dict):
        status_text = str(status_obj.get("text", "В обробці"))
        is_cancelled = bool(status_obj.get("cancelled", False))
    else:
        status_text = str(status_obj) or "В обробці"
        is_cancelled = False

    source_items = safe_order.get("items") or safe_order.get("line_items") or []
    if not isinstance(source_items, list):
        source_items = []

    formatted_items = []
    for item in source_items:
        if not isinstance(item, dict):
            continue
        title = item.get("title") or item.get("name", "")
        quantity = item.get("quantity") or item.get("qty", 1)
        try:
            quantity = float(quantity)
            if quantity.is_integer():
                quantity = int(quantity)
        except (ValueError, TypeError):
            quantity = 1

        lt = item.get("line_total")
        if isinstance(lt, dict):
            lt_formatted = str(lt.get("formatted") or lt.get("amount", ""))
        elif isinstance(lt, (int, float, str)):
            lt_formatted = str(lt)
        else:
            price = item.get("price") or item.get("unit_price", 0)
            lt_formatted = str(price * quantity) if price else ""

        formatted_items.append({
            "title": str(title),
            "quantity": quantity,
            "line_total": lt_formatted,
            "sku": str(item.get("sku", "")),
            "item_id": str(item.get("item_id", "") or item.get("id", "")),
        })

    customer_name = str(safe_order.get("customer_name") or "")

    return {
        "customer_name": customer_name,
        "phone": str(safe_order.get("phone", "")),
        "email": str(safe_order.get("email", "")),
        "delivery_address": full_address,
        "city": city,
        "region": region,
        "postal_code": postal_code,
        "country": country,
        "delivery_method": str(safe_order.get("delivery_method", "")),
        "total_amount": total_formatted,
        "currency": currency,
        "order_number": str(safe_order.get("order_number") or safe_order.get("id", "")),
        "status": status_text,
        "cancelled": is_cancelled,
        "note": str(safe_order.get("note") or safe_order.get("notes", "")),
        "payment_gateway": str(safe_order.get("payment_gateway") or safe_order.get("gateway", "")),
        "items": formatted_items,
        "ai_summary": "",
        "chat_summary": str(safe_order.get("chat_history", "")),
        "market_currency_data": {}
    }


def fill_missing_fields(report: dict, extracted: dict) -> None:
    """Безпечно доповнює звіт даними, які витягнув ШІ-агент."""
    if not isinstance(extracted, dict):
        return

    for field in (
        "customer_name", "phone", "email", "delivery_address", "city", "region",
        "postal_code", "country", "delivery_method", "total_amount", "currency", 
        "order_number", "status", "note", "payment_gateway", "chat_summary"
    ):
        value = extracted.get(field)
        if not report.get(field) and isinstance(value, str) and value.strip():
            if field == "customer_name" and value.strip().startswith("#"):
                continue
            report[field] = value.strip()

    if isinstance(extracted.get("cancelled"), bool):
        report["cancelled"] = extracted.get("cancelled")

    if isinstance(extracted.get("market_currency_data"), dict):
        report["market_currency_data"] = extracted.get("market_currency_data")

    if not report["items"] and isinstance(extracted.get("items"), list):
        items = []
        for item in extracted["items"]:
            if not isinstance(item, dict):
                continue
            title = item.get("title")
            quantity = item.get("quantity")
            line_total = item.get("line_total")
            if isinstance(title, str) and isinstance(quantity, (int, float)) and not isinstance(quantity, bool):
                items.append({
                    "title": title.strip(),
                    "quantity": quantity,
                    "line_total": str(line_total).strip() if line_total else "",
                    "sku": str(item.get("sku", "")).strip(),
                    "item_id": str(item.get("item_id", "")).strip(),
                })
        if items:
            report["items"] = items


async def analyze_order(order: dict, chat_messages: str) -> dict:
    """Головна точка входу з інтегрованим Redis-кешуванням відповідей ШІ."""
    
    cache_payload = json.dumps({"order": order, "chat": chat_messages}, sort_keys=True, ensure_ascii=False)
    cache_key = f"ai_cache:{hashlib.sha256(cache_payload.encode('utf-8')).hexdigest()}"
    
    redis = await get_redis_cache()
    if redis:
        try:
            cached_result = await redis.get(cache_key)
            if cached_result:
                logger.info("⚡ Знайдено кешовану відповідь ШІ у Redis (економія токенів).")
                return json.loads(cached_result)
        except Exception as e:
            logger.warning(f"Помилка читання кешу з Redis: {e}")

    report = standard_fields(order)
    
    currency_data = await fetch_realtime_currency_rates()
    report["market_currency_data"] = currency_data

    is_chat_request = (isinstance(order, dict) and order.get("id") == "chat-session")

    try:
        if is_chat_request:
            reply_text = await MultiAgentSystem.run_sales_agent(chat_messages, "")
            result = {"recommendation": reply_text, "ai_summary": reply_text}
        else:
            if not report.get("order_number") or not report.get("customer_name"):
                raw_str = json.dumps(order, ensure_ascii=False)
                parsed_data = await MultiAgentSystem.run_fallback_parser_agent(raw_str)
                if parsed_data:
                    fill_missing_fields(report, parsed_data)

            extracted = await MultiAgentSystem.run_analytics_agent(order, chat_messages)
            fill_main_extracted = fill_missing_fields(report, extracted)
            
            summary = extracted.get("ai_summary")
            report["ai_summary"] = " ".join(summary.split()) if isinstance(summary, str) else ""

            chat_sum = extracted.get("chat_summary")
            if isinstance(chat_sum, str) and chat_sum.strip():
                report["chat_summary"] = " ".join(chat_sum.split())

            result = report

        if redis:
            try:
                await redis.set(cache_key, json.dumps(result, ensure_ascii=False), ex=900)
            except Exception as e:
                logger.warning(f"Помилка запису кешу в Redis: {e}")

        return result

    except Exception as error:
        logger.error("[ERROR] Помилка обробки замовлення через агента: %s", error, exc_info=True)
        if is_chat_request:
            return {"recommendation": "Вітаю! У мене невеликий технічний збій, але я готовий допомогти."}
        report["ai_summary"] = "ШІ тимчасово недоступний. Перевірте замовлення вручну."
        return report