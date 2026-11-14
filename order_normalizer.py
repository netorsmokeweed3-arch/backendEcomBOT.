"""Універсальний обробник замовлень для SaaS-платформи (Enterprise Edition).
Архітектура на базі паттерну Адаптер, Pydantic v2 та AI Self-Healing з Агентом-Супервізором.
"""

import re
import logging
import json
from typing import Any, Optional, List, Dict, Tuple
from pydantic import BaseModel, Field, ValidationError, field_validator

# Імпортуємо наші адаптери з папки adapters/
from adapters.prom import PromAdapter
from adapters.shopify import ShopifyAdapter
from adapters.horoshop import HoroshopAdapter
from adapters.keycrm import KeyCrmAdapter
from adapters.remonline import RemonlineAdapter
from adapters.epicentr import EpicentrAdapter

# Імпортуємо систему агентів та Redis для кешування мапінгів
from agents import MultiAgentSystem
from redis_pool import get_redis

logger = logging.getLogger(__name__)

EMPTY = (None, "", (), {}, "None", "null", "N/A", "n/a")

CURRENCY_PREFIX = {
    "USD": "$",
    "EUR": "€",
    "GBP": "£",
    "UAH": "₴"
}


# ==========================================
# 1. PYDANTIC v2 SCHEMAS
# ==========================================

class OrderItem(BaseModel):
    id: str = Field(default="", description="ID товару")
    sku: str = Field(default="UNKNOWN_SKU", description="Артикул товару")
    title: str = Field(default="Товар без назви", description="Назва товару")
    quantity: int = Field(default=1, ge=0, description="Кількість")
    unit_price: Dict[str, Any] = Field(default_factory=dict, description="Ціна за одиницю")
    line_total: Dict[str, Any] = Field(default_factory=dict, description="Загальна вартість позиції")

    @field_validator('quantity', mode='before')
    @classmethod
    def parse_quantity(cls, v: Any) -> int:
        try:
            return int(v) if v is not None else 1
        except (ValueError, TypeError):
            return 1


class UnifiedOrder(BaseModel):
    shop: str = Field(..., description="Домен або ідентифікатор магазину")
    id: str = Field(..., description="Унікальний ID замовлення")
    order_number: str = Field(default="", description="Номер замовлення")
    created_at: str = Field(default="", description="Дата створення")
    currency: str = Field(default="UAH", description="Валюта замовлення")
    customer_name: str = Field(default="Гість", description="Ім'я покупця")
    phone: str = Field(default="", description="Телефон покупця")
    email: str = Field(default="", description="Email покупця")
    delivery_method: str = Field(default="", description="Спосіб доставки")
    delivery_address: Dict[str, Any] = Field(default_factory=dict, description="Адреса доставки")
    total_amount: Dict[str, Any] = Field(default_factory=dict, description="Загальна сума")
    items: List[OrderItem] = Field(default_factory=list, description="Список товарів")
    status: Dict[str, Any] = Field(default_factory=dict, description="Статус замовлення")
    chat_history: str = Field(default="", description="Історія чату")


# ==========================================
# 2. ORDER NORMALIZER COORDINATOR (AI Self-Healing & Telemetry)
# ==========================================

class OrderNormalizer:
    def __init__(self):
        # Повний реєстр платформ та їхніх адаптерів
        self._adapters = {
            "prom": PromAdapter(),
            "shopify": ShopifyAdapter(),
            "horoshop": HoroshopAdapter(),
            "keycrm": KeyCrmAdapter(),
            "remonline": RemonlineAdapter(),
            "epicentr": EpicentrAdapter(),
        }

    async def normalize_async(self, platform: str, raw_data: dict) -> dict:
        """Асинхронна нормалізація з інтегрованим AI Self-Healing та перевіркою Супервізором."""
        platform_key = platform.lower()
        adapter = self._adapters.get(platform_key)
        
        if not adapter:
            logger.warning(f"⚠️ Немає зареєстрованого адаптера для платформи '{platform}'. Використовується шлях без адаптації.")
            return raw_data if isinstance(raw_data, dict) else {}

        # 1. Виконуємо первинну нормалізацію через адаптер
        normalized_data, unknown_keys = adapter.normalize(raw_data, platform_key)

        # 2. Якщо виявлено дрейф схем (unknown_keys) — запускаємо AI Self-Healing з Агентом 5 та Агентом 6
        if unknown_keys:
            logger.warning(f"🚨 [API Schema Drift] Платформа '{platform}' повернула нові невідомі поля: {unknown_keys}")
            
            redis = await get_redis()
            for key in unknown_keys:
                sample_val = raw_data.get(key)
                cache_mapping_key = f"self_heal:{platform_key}:{key}"
                
                standard_field = None
                
                # Перевіряємо Redis-кеш мапінгів
                if redis:
                    cached_field = await redis.get(cache_mapping_key)
                    if cached_field:
                        standard_field = cached_field.decode('utf-8') if isinstance(cached_field, bytes) else cached_field

                # Якщо в кеші немає — запитуємо Агента 5 (Self-Healing)
                if not standard_field:
                    heal_res = await MultiAgentSystem.run_self_healing_agent(key, sample_val, platform_key)
                    standard_field = heal_res.get("standard_field")
                    confidence = float(heal_res.get("confidence", 0.0))
                    
                    if standard_field:
                        # Сеньйорська логіка: Зелена зона (>= 0.85) довіряємо одразу
                        if confidence < 0.85 and confidence >= 0.5:
                            # Жовта зона (0.5 - 0.85) — залучаємо Агента-Супервізора (Агента 6)
                            logger.info(f"⚠️ [Supervisor Review] Поле '{key}' має середню впевненість {confidence}. Запуск Супервізора...")
                            review_context = {
                                "platform": platform_key,
                                "unknown_key": key,
                                "sample_value": sample_val,
                                "suggested_mapping": standard_field,
                                "initial_confidence": confidence
                            }
                            verdict = await MultiAgentSystem.run_supervisor_agent(
                                task_context="Validate API Field Self-Healing Mapping",
                                data_to_review=review_context
                            )
                            
                            if verdict.get("approved") is True:
                                standard_field = verdict.get("corrected_field") or standard_field
                                logger.info(f"🛡️ [Supervisor Approved] Мапінг підтверджено для '{key}' -> '{standard_field}'")
                            else:
                                logger.error(f"❌ [Supervisor Rejected] Мапінг відхилено: {verdict.get('reason')}")
                                standard_field = None
                        elif confidence < 0.5:
                            standard_field = None

                        # Якщо мапінг успішно підтверджено — кешуємо в Redis на 30 днів (2592000 сек)
                        if standard_field and redis:
                            await redis.set(cache_mapping_key, standard_field, ex=2592000)

                # Застосовуємо знайдене поле до нормалізованих даних
                if standard_field and standard_field in normalized_data:
                    normalized_data[standard_field] = sample_val

        return normalized_data

# Глобальний екземпляр нормалізатора
_normalizer = OrderNormalizer()


class OrderAPIError(Exception):
    def __init__(self, message: str, status: int = 400):
        super().__init__(message)
        self.status = status


def normalize_shop(raw: Any) -> Optional[str]:
    if not isinstance(raw, str):
        return None
    value = re.sub(r"^https?://", "", raw.strip().lower()).split("/")[0]
    return value if value else None


def get_path(data: Any, path_tuple: Tuple[str, ...]) -> Any:
    """Швидкий обхід вкладеного JSON без зайвого split() завдяки pre-split кортежам."""
    current = data
    for part in path_tuple:
        if isinstance(current, dict):
            current = current.get(part)
        elif isinstance(current, list) and part.isdigit() and int(part) < len(current):
            current = current[int(part)]
        else:
            return None
    return None if current in EMPTY else current


def find(data: Any, field: str, platform: str = "prom") -> Any:
    """Шукає поле через адаптер відповідної платформи."""
    adapter = _normalizer._adapters.get(platform.lower())
    if not adapter:
        return None
    
    for path_tuple in adapter.get_field_paths(field):
        value = get_path(data, path_tuple)
        if value is not None:
            return value
    return None


def to_text(value: Any) -> str:
    if isinstance(value, bool) or value is None:
        return ""
    if isinstance(value, (str, int, float)):
        return str(value).strip()
    return ""


def to_number(value: Any) -> Optional[float]:
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, dict):
        return to_number(value.get("amount", value.get("value")))
    try:
        return float(str(value).replace(",", ".").strip())
    except ValueError:
        return None


def money(amount: Any, currency: str) -> dict:
    code = (currency or "UAH").upper()
    value = round(to_number(amount) or 0.0, 2)
    if code in CURRENCY_PREFIX:
        formatted = f"{CURRENCY_PREFIX[code]}{value:.2f}"
    else:
        formatted = f"{value:.2f} {'грн' if code == 'UAH' else code}"
    return {"amount": value, "currency": code, "formatted": formatted}


def normalize_items(raw_items: Any, currency: str, platform: str) -> list:
    items = []
    if not isinstance(raw_items, list):
        return items
    for item in raw_items:
        if not isinstance(item, dict):
            continue
        title = to_text(find(item, "item_title", platform)) or "Товар"
        variant = to_text(find(item, "item_variant", platform))
        if variant and variant.lower() != "default title":
            title = f"{title} ({variant})"
        quantity_value = to_number(find(item, "item_quantity", platform))
        quantity = int(quantity_value) if quantity_value and quantity_value > 0 else 1
        unit_price = to_number(find(item, "item_price", platform)) or 0.0
        line_total = to_number(find(item, "item_line_total", platform))
        items.append({
            "id": to_text(find(item, "item_id", platform)),
            "title": title,
            "sku": to_text(find(item, "item_sku", platform)),
            "quantity": quantity,
            "unit_price": money(unit_price, currency),
            "line_total": money(line_total if line_total is not None else unit_price * quantity, currency),
        })
    return items


def string_address(raw: Any) -> str:
    if not isinstance(raw, dict):
        return ""
    for key in ("delivery_address", "deliveryAddress", "shipping_address", "shippingAddress", "address"):
        value = raw.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return ""


async def normalize_order_async(raw: dict, shop: str, platform: str = "prom") -> dict:
    """Головна асинхронна функція нормалізації замовлення з підтримкою AI Self-Healing та Супервізора."""
    if not isinstance(raw, dict):
        raw = {}

    # Запускаємо асинхронну нормалізацію та самовідновлення полів через наш клас
    await _normalizer.normalize_async(platform, raw)

    currency = to_text(find(raw, "currency", platform)) or "UAH"

    billing = raw.get("billing", {}) if isinstance(raw.get("billing"), dict) else {}
    shipping = raw.get("shipping", {}) if isinstance(raw.get("shipping"), dict) else {}

    first = (
        to_text(find(raw, "first_name", platform)) 
        or to_text(get_path(raw, ("customer", "first_name")))
        or to_text(billing.get("first_name"))
        or to_text(shipping.get("first_name"))
    )
    last = (
        to_text(find(raw, "last_name", platform)) 
        or to_text(get_path(raw, ("customer", "last_name")))
        or to_text(billing.get("last_name"))
        or to_text(shipping.get("last_name"))
    )
    customer_name = (
        " ".join(part for part in (first, last) if part) 
        or to_text(billing.get("company"))
        or to_text(get_path(raw, ("customer", "name")))
        or to_text(find(raw, "customer_name", platform))
        or "Гість"
    )

    street = (
        to_text(get_path(raw, ("shipping_address", "address1"))) 
        or to_text(find(raw, "street", platform))
        or to_text(shipping.get("address_1"))
        or to_text(billing.get("address_1"))
    )
    street2 = (
        to_text(get_path(raw, ("shipping_address", "address2"))) 
        or to_text(find(raw, "street2", platform))
        or to_text(shipping.get("address_2"))
        or to_text(billing.get("address_2"))
    )
    full_street = " ".join(part for part in (street, street2) if part) or string_address(raw)

    city = (
        to_text(get_path(raw, ("shipping_address", "city"))) 
        or to_text(find(raw, "city", platform))
        or to_text(shipping.get("city"))
        or to_text(billing.get("city"))
    )
    region = (
        to_text(get_path(raw, ("shipping_address", "province"))) 
        or to_text(find(raw, "region", platform))
        or to_text(shipping.get("state"))
        or to_text(billing.get("state"))
    )
    postal_code = (
        to_text(get_path(raw, ("shipping_address", "zip"))) 
        or to_text(find(raw, "postal_code", platform))
        or to_text(shipping.get("postcode"))
        or to_text(billing.get("postcode"))
    )
    country = (
        to_text(get_path(raw, ("shipping_address", "country"))) 
        or to_text(find(raw, "country", platform))
        or to_text(shipping.get("country"))
        or to_text(billing.get("country"))
    )
    
    full_address = ", ".join(part for part in (country, region, city, full_street, postal_code) if part)

    phone = to_text(find(raw, "phone", platform)) or to_text(billing.get("phone")) or to_text(shipping.get("phone"))
    email = to_text(find(raw, "email", platform)) or to_text(billing.get("email")) or to_text(shipping.get("email"))
    chat_history = raw.get("chat_history") or raw.get("dialog_history") or ""

    base_dict = {
        "shop": shop,
        "id": to_text(get_path(raw, ("id",))) or to_text(find(raw, "order_number", platform)),
        "order_number": to_text(find(raw, "order_number", platform)),
        "created_at": to_text(find(raw, "created_at", platform)),
        "currency": currency.upper(),
        "customer_name": customer_name,
        "phone": phone,
        "email": email,
        "delivery_method": to_text(find(raw, "delivery_method", platform)),
        "delivery_address": {
            "street": full_street,
            "city": city,
            "region": region,
            "postal_code": postal_code,
            "country": country,
            "full": full_address,
        },
        "total_amount": money(find(raw, "total_amount", platform), currency),
        "items": normalize_items(find(raw, "items", platform), currency, platform),
        "status": {
            "text": to_text(find(raw, "status", platform)) or "В обробці",
            "cancelled": bool(find(raw, "cancelled", platform)),
        },
        "chat_history": str(chat_history),
    }

    try:
        validated = UnifiedOrder(**base_dict)
        return validated.model_dump()
    except ValidationError as ve:
        logger.error(f"Pydantic Validation Error for shop {shop}: {ve.errors()}")
        return base_dict