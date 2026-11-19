"""Сервісний шар для обробки замовлень та взаємодії з ШІ-агентами.
Містить стандартизацію полів, self-healing мапінг невідомих ключів через Агентів 5 та 6,
заповнення пропущених даних та кешування відповідей у Redis.
"""

import json
import logging
import hashlib
from typing import Optional, Dict, Any, Callable, Awaitable, Tuple
from redis_pool import get_redis
from agents import MultiAgentSystem, fetch_realtime_currency_rates, MODEL as AI_MODEL_NAME

logger = logging.getLogger(__name__)

# Дозволені цільові поля звіту, які може заповнювати Self-Healing після схвалення Supervisor
ALLOWED_HEALED_FIELDS = {
    "order_number",
    "customer_name",
    "phone",
    "email",
    "delivery_address",
    "city",
    "region",
    "postal_code",
    "country",
    "delivery_method",
    "total_amount",
    "currency",
    "status",
    "cancelled",
    "note",
    "payment_gateway",
}

# Мапінг полів зі списку Агента 5 у поля стандартного звіту report
AGENT5_TO_REPORT_FIELD: Dict[str, str] = {
    "order_number": "order_number",
    "created_at": "",
    "customer_name": "customer_name",
    "first_name": "customer_name",
    "last_name": "customer_name",
    "phone": "phone",
    "email": "email",
    "street": "delivery_address",
    "street2": "delivery_address",
    "city": "city",
    "region": "region",
    "postal_code": "postal_code",
    "country": "country",
    "delivery_method": "delivery_method",
    "currency": "currency",
    "total_amount": "total_amount",
    "status": "status",
    "cancelled": "cancelled",
    "note": "note",
    "payment_gateway": "payment_gateway",
}

MIN_CONFIDENCE_THRESHOLD = 0.70

# In-memory кеш затверджених мапінгів: {(platform, raw_key): report_field_or_none}
_memory_field_mappings: Dict[Tuple[str, str], Optional[str]] = {}

# Зовнішні хуки для роботи з БД (підключаються з бекенду за потреби)
LoadMappingFn = Callable[[str, str], Awaitable[Optional[str]]]
SaveMappingFn = Callable[[str, str, Optional[str], float, str], Awaitable[None]]

_load_mapping_hook: Optional[LoadMappingFn] = None
_save_mapping_hook: Optional[SaveMappingFn] = None


def configure_self_healing_storage(
    load_fn: Optional[LoadMappingFn] = None,
    save_fn: Optional[SaveMappingFn] = None,
) -> None:
    """Реєструє функції читання та збереження затверджених мапінгів у БД."""
    global _load_mapping_hook, _save_mapping_hook
    if load_fn is not None:
        _load_mapping_hook = load_fn
    if save_fn is not None:
        _save_mapping_hook = save_fn


def _normalize_agent_field_to_report(field_name: Optional[str]) -> Optional[str]:
    """Переводить назву поля від Агента 5 / Агента 6 у валідне поле звіту або повертає None."""
    if not isinstance(field_name, str):
        return None
    cleaned = field_name.strip()
    if not cleaned or cleaned.lower() == "null":
        return None

    mapped = AGENT5_TO_REPORT_FIELD.get(cleaned, cleaned)
    if mapped in ALLOWED_HEALED_FIELDS:
        return mapped
    return None


def _apply_healed_value(report: Dict[str, Any], target_field: str, raw_value: Any) -> None:
    """Безпечно підставляє розпізнане значення у report, якщо поле ще не заповнене."""
    if raw_value is None or target_field not in ALLOWED_HEALED_FIELDS:
        return

    if target_field == "cancelled":
        if isinstance(raw_value, bool):
            report["cancelled"] = raw_value
        elif isinstance(raw_value, str):
            low = raw_value.strip().lower()
            if low in ("true", "1", "yes", "cancelled", "скасовано"):
                report["cancelled"] = True
            elif low in ("false", "0", "no", "active"):
                report["cancelled"] = False
        return

    if target_field == "total_amount":
        current_total = report.get("total_amount")
        if current_total is None or str(current_total).strip() == "":
            if isinstance(raw_value, (int, float)) and not isinstance(raw_value, bool):
                report["total_amount"] = str(raw_value)
            elif isinstance(raw_value, str) and raw_value.strip():
                report["total_amount"] = raw_value.strip()
        return

    current_val = report.get(target_field)
    if current_val is None or (isinstance(current_val, str) and not current_val.strip()):
        if isinstance(raw_value, (str, int, float)) and not isinstance(raw_value, bool):
            val_str = str(raw_value).strip()
            if val_str:
                if target_field == "delivery_address" and isinstance(current_val, str) and current_val.strip():
                    report["delivery_address"] = f"{current_val.strip()}, {val_str}"
                else:
                    report["delivery_address" if target_field == "delivery_address" else target_field] = val_str


async def heal_unknown_order_fields(
    order: Dict[str, Any],
    report: Dict[str, Any],
    platform: Optional[str] = None,
) -> Dict[str, Any]:
    """Проходить по незнайомих ключах замовлення через Агента 5 (Self-Healing)
    та перевіряє результат через Агента 6 (Supervisor) перед застосуванням і кешуванням.
    """
    if not isinstance(order, dict) or not order:
        return report

    norm_platform = (platform or str(order.get("platform") or order.get("source") or "generic")).strip().lower()

    known_keys = set(report.keys()) | {
        "id", "shipping_address", "delivery_address", "line_items", "items",
        "chat_history", "market_currency_data", "ai_model_used", "platform", "source"
    }

    for raw_key, sample_val in order.items():
        if not isinstance(raw_key, str) or not raw_key.strip():
            continue
        key_clean = raw_key.strip()

        if key_clean in known_keys:
            continue

        if isinstance(sample_val, (dict, list)) or sample_val is None:
            continue

        mem_key = (norm_platform, key_clean)

        if mem_key in _memory_field_mappings:
            cached_field = _memory_field_mappings[mem_key]
            if cached_field:
                _apply_healed_value(report, cached_field, sample_val)
            continue

        if _load_mapping_hook is not None:
            try:
                db_field = await _load_mapping_hook(norm_platform, key_clean)
                if db_field is not None:
                    normalized_db_field = _normalize_agent_field_to_report(db_field)
                    _memory_field_mappings[mem_key] = normalized_db_field
                    if normalized_db_field:
                        _apply_healed_value(report, normalized_db_field, sample_val)
                    continue
            except Exception as e:
                logger.warning("[SELF-HEALING] Помилка читання мапінгу з БД для '%s': %s", key_clean, e)

        healing_res = await MultiAgentSystem.run_self_healing_agent(
            unknown_key=key_clean,
            sample_value=sample_val,
            platform=norm_platform,
        )
        proposed_field = healing_res.get("standard_field")
        try:
            confidence = float(healing_res.get("confidence", 0.0))
        except (ValueError, TypeError):
            confidence = 0.0

        supervisor_res = await MultiAgentSystem.run_supervisor_agent(
            task_context="self_healing_field_mapping",
            data_to_review={
                "platform": norm_platform,
                "raw_key": key_clean,
                "sample_value": sample_val,
                "proposed_standard_field": proposed_field,
                "confidence": confidence,
            },
        )

        approved = bool(supervisor_res.get("approved", False))
        reason = str(supervisor_res.get("reason", ""))
        corrected_field = supervisor_res.get("corrected_field")

        final_candidate = corrected_field if corrected_field else proposed_field
        final_report_field = _normalize_agent_field_to_report(final_candidate)

        if approved and final_report_field and confidence >= MIN_CONFIDENCE_THRESHOLD:
            _memory_field_mappings[mem_key] = final_report_field
            _apply_healed_value(report, final_report_field, sample_val)
            logger.info(
                "[SELF-HEALING APPROVED] platform=%s key='%s' -> '%s' (conf=%.2f): %s",
                norm_platform, key_clean, final_report_field, confidence, reason
            )
            if _save_mapping_hook is not None:
                try:
                    await _save_mapping_hook(norm_platform, key_clean, final_report_field, confidence, reason)
                except Exception as e:
                    logger.warning("[SELF-HEALING] Не вдалося зберегти мапінг у БД для '%s': %s", key_clean, e)
        else:
            _memory_field_mappings[mem_key] = None
            logger.info(
                "[SELF-HEALING REJECTED] platform=%s key='%s' (candidate=%s, conf=%.2f, approved=%s): %s",
                norm_platform, key_clean, final_candidate, confidence, approved, reason
            )

    return report


def standard_fields(order: dict) -> dict:
    """Нормалізує та стандартизує сирі дані замовлення у єдиний формат звіту."""
    safe_order = order if isinstance(order, dict) else {}
    address = safe_order.get("delivery_address") or safe_order.get("shipping_address") or {}
    raw_total = safe_order.get("total_amount")
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

    if raw_total is None:
        total_formatted = ""
    elif isinstance(raw_total, (int, float, str)) and not isinstance(raw_total, bool):
        total_formatted = str(raw_total)
    elif isinstance(raw_total, dict):
        val = raw_total.get("formatted") if raw_total.get("formatted") is not None else raw_total.get("amount")
        total_formatted = str(val) if val is not None else ""
    else:
        total_formatted = ""

    currency = str(
        safe_order.get("currency")
        or (raw_total.get("currency") if isinstance(raw_total, dict) else "")
        or ""
    )

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
        raw_qty = item.get("quantity") if item.get("quantity") is not None else item.get("qty", 1)
        try:
            quantity = float(raw_qty)
            if quantity.is_integer():
                quantity = int(quantity)
        except (ValueError, TypeError):
            quantity = 1

        lt = item.get("line_total")
        if isinstance(lt, dict):
            lt_val = lt.get("formatted") if lt.get("formatted") is not None else lt.get("amount")
            lt_formatted = str(lt_val) if lt_val is not None else ""
        elif lt is not None and isinstance(lt, (int, float, str)) and not isinstance(lt, bool):
            lt_formatted = str(lt)
        else:
            price = item.get("price") if item.get("price") is not None else item.get("unit_price")
            try:
                lt_formatted = str(float(price) * quantity) if price is not None else ""
            except (ValueError, TypeError):
                lt_formatted = ""

        formatted_items.append({
            "title": str(title),
            "quantity": quantity,
            "line_total": lt_formatted,
            "sku": str(item.get("sku", "")),
            "item_id": str(item.get("item_id") or item.get("id") or ""),
        })

    customer_name = str(safe_order.get("customer_name") or "")

    return {
        "customer_name": customer_name,
        "phone": str(safe_order.get("phone") or ""),
        "email": str(safe_order.get("email") or ""),
        "delivery_address": full_address,
        "city": city,
        "region": region,
        "postal_code": postal_code,
        "country": country,
        "delivery_method": str(safe_order.get("delivery_method") or ""),
        "total_amount": total_formatted,
        "currency": currency,
        "order_number": str(safe_order.get("order_number") or safe_order.get("id") or ""),
        "status": status_text,
        "cancelled": is_cancelled,
        "note": str(safe_order.get("note") or safe_order.get("notes") or ""),
        "payment_gateway": str(safe_order.get("payment_gateway") or safe_order.get("gateway") or ""),
        "items": formatted_items,
        "ai_summary": "",
        "chat_summary": str(safe_order.get("chat_history") or ""),
        "market_currency_data": {},
        "ai_model_used": AI_MODEL_NAME,
    }


def fill_missing_fields(report: dict, extracted: dict) -> None:
    """Заповнює порожні поля звіту даними, витягнутими ШІ-агентами."""
    if not isinstance(extracted, dict):
        return

    for field in (
        "customer_name", "phone", "email", "delivery_address", "city", "region",
        "postal_code", "country", "delivery_method", "total_amount", "currency",
        "order_number", "status", "note", "payment_gateway", "chat_summary"
    ):
        value = extracted.get(field)
        current = report.get(field)
        is_empty = current is None or (isinstance(current, str) and not current.strip())
        if is_empty and isinstance(value, (str, int, float)) and not isinstance(value, bool):
            val_str = str(value).strip()
            if val_str:
                if field == "customer_name" and val_str.startswith("#"):
                    continue
                report[field] = val_str

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
                    "line_total": str(line_total).strip() if line_total is not None else "",
                    "sku": str(item.get("sku") or "").strip(),
                    "item_id": str(item.get("item_id") or "").strip(),
                })
        if items:
            report["items"] = items


async def analyze_order(
    order: dict,
    chat_messages: str,
    user_id: Optional[int] = None,
    shop_domain: Optional[str] = None,
    platform: Optional[str] = None,
) -> dict:
    """Головна точка входу з Self-Healing + Supervisor, аналітикою та Redis-кешуванням."""
    cache_payload = json.dumps({
        "user_id": user_id,
        "shop_domain": shop_domain,
        "platform": platform,
        "order": order,
        "chat": chat_messages,
        "model": AI_MODEL_NAME,
    }, sort_keys=True, ensure_ascii=False)

    cache_key = f"ai_cache:{hashlib.sha256(cache_payload.encode('utf-8')).hexdigest()}"

    redis = await get_redis()
    if redis:
        try:
            cached_result = await redis.get(cache_key)
            if cached_result:
                logger.info("⚡ Знайдено ізольовану кешовану відповідь ШІ у Redis (модель: %s).", AI_MODEL_NAME)
                return json.loads(cached_result)
        except Exception as e:
            logger.warning("Помилка читання кешу з Redis: %s", e)

    report = standard_fields(order)

    currency_data = await fetch_realtime_currency_rates()
    report["market_currency_data"] = currency_data

    is_chat_request = (isinstance(order, dict) and order.get("id") == "chat-session")

    try:
        if is_chat_request:
            reply_text = await MultiAgentSystem.run_sales_agent(chat_messages, "")
            result = {"recommendation": reply_text, "ai_summary": reply_text, "ai_model_used": AI_MODEL_NAME}
        else:
            if isinstance(order, dict):
                await heal_unknown_order_fields(order, report, platform=platform or shop_domain)

            if not report.get("order_number") or not report.get("customer_name"):
                raw_str = json.dumps(order, ensure_ascii=False)
                parsed_data = await MultiAgentSystem.run_fallback_parser_agent(raw_str)
                if parsed_data:
                    fill_missing_fields(report, parsed_data)

            extracted = await MultiAgentSystem.run_analytics_agent(order, chat_messages)
            fill_missing_fields(report, extracted)

            summary = extracted.get("ai_summary")
            report["ai_summary"] = " ".join(summary.split()) if isinstance(summary, str) else ""

            chat_sum = extracted.get("chat_summary")
            if isinstance(chat_sum, str) and chat_sum.strip():
                report["chat_summary"] = " ".join(chat_sum.strip().split())

            result = report

        if redis:
            try:
                await redis.set(cache_key, json.dumps(result, ensure_ascii=False), ex=900)
            except Exception as e:
                logger.warning("Помилка запису кешу в Redis: %s", e)

        return result

    except Exception as error:
        logger.error("[ERROR] Помилка обробки замовлення через агента (модель %s): %s", AI_MODEL_NAME, error, exc_info=True)
        if is_chat_request:
            return {"recommendation": "Вітаю! У мене невеликий технічний збій, але я готовий допомогти.", "ai_model_used": AI_MODEL_NAME}
        report["ai_summary"] = "ШІ тимчасово недоступний. Перевірте замовлення вручну."
        return report