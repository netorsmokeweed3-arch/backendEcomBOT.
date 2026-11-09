"""Універсальний обробник замовлень для SaaS-платформи (Enterprise Edition).
Містить повну базу ключів-синонімів для всіх магазинів і маркетплейсів,
Pydantic-валідацію, асинхронний збір даних та захист від обривів мережі (Retry).
"""

import os
import re
import asyncio
import logging
from functools import lru_cache
from typing import Any, Optional, List, Dict
from pydantic import BaseModel, Field, ValidationError, field_validator
import httpx
from cryptography.fernet import Fernet

logger = logging.getLogger("ecom_engine")
logger.setLevel(logging.INFO)

# Ключ шифрування токенів у сховищі
ENCRYPTION_KEY = os.getenv("APP_ENCRYPTION_KEY", Fernet.generate_key())
cipher_suite = Fernet(ENCRYPTION_KEY)

# Безпечний кортеж порожніх значень для перевірок
EMPTY = (None, "", (), {}, "None", "null", "N/A", "n/a")

CURRENCY_PREFIX = {
    "USD": "$",
    "EUR": "€",
    "GBP": "£",
    "UAH": "₴"
}


# ==========================================
# 1. PYDANTIC SCHEMAS (Crash-Free Guarantee)
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
    chat_history: str = Field(default="", description="Історія чату або діалогу")


# ==========================================
# 2. FULL STATIC FIELD SYNONYMS (Усі ключі для всіх магазинів)
# ==========================================

FIELD_SYNONYMS = {
    "order_number": [
        "order_number", "orderNumber", "number", "name", "order_id", "id", 
        "order_no", "orderNo", "reference", "code", "invoice_no", "invoiceNumber",
        "order_reference", "external_id", "externalId", "display_number", "displayNumber",
        "order_code", "orderCode", "increment_id", "incrementId", "num", "order_num",
        "zakaz_id", "nomer_zakazu", "nomer", "transaction_id", "transactionId", "order_index",
        "order.id", "order.number", "order.name", "attributes.number", "data.id", "data.order_number"
    ],
    "created_at": [
        "created_at", "createdAt", "date_created", "date", "created", 
        "order_date", "date_added", "timestamp", "inserted_at", "processed_at",
        "processedAt", "date_modified", "updated_at", "updatedAt", "date_purchased",
        "order.created_at", "order.date", "attributes.created_at", "data.created_at"
    ],
    "customer_name": [
        "customer.full_name", "customer.fullName", "customer.name", "customer_name", "customerName",
        "client_name", "buyer_name", "full_name", "fullName", "buyer.full_name",
        "buyer.name", "client.name", "recipient_name", "shipping_address.name", "shippingAddress.name",
        "billing_address.name", "billingAddress.name", "shipping_name", "billing_name",
        "contact_name", "contactName", "receiver", "recipient", "pib", "fio"
    ],
    "first_name": [
        "customer.first_name", "customer.firstName", "buyer.first_name", "client.first_name",
        "shipping_address.first_name", "shippingAddress.first_name", "billing_address.first_name",
        "client_first_name", "first_name", "firstName", "fname", "given_name", "givenName", "imya"
    ],
    "last_name": [
        "customer.last_name", "customer.lastName", "buyer.last_name", "client.last_name",
        "shipping_address.last_name", "shippingAddress.last_name", "billing_address.last_name",
        "client_last_name", "last_name", "lastName", "lname", "surname", "family_name", "prizvyshche"
    ],
    "phone": [
        "shipping_address.phone", "shippingAddress.phone", "delivery_address.phone",
        "customer.phone", "buyer.phone", "client.phone", "phone",
        "billing_address.phone", "billingAddress.phone", "delivery.phone", "telephone", 
        "phone_number", "phoneNumber", "contact_phone", "client_phone", "mobile", "cellphone"
    ],
    "email": [
        "email", "customer.email", "buyer.email", "client.email", "shipping_address.email", 
        "shippingAddress.email", "billing_address.email", "billingAddress.email", "client_email", 
        "customer_email", "mail", "contact_email", "contactEmail", "customer.mail", "e_mail"
    ],
    "street": [
        "shipping_address.address1", "shippingAddress.address1", "delivery_address.street",
        "deliveryAddress.street", "delivery.address", "address1", "address", "street", 
        "shipping_address.street", "billing_address.address1", "billingAddress.address1", "line1"
    ],
    "street2": [
        "shipping_address.address2", "shippingAddress.address2", "delivery_address.address2",
        "deliveryAddress.address2", "address2", "apartment", "apt", "suite", "unit", 
        "billing_address.address2", "billingAddress.address2", "line2", "building", "house"
    ],
    "city": [
        "shipping_address.city", "shippingAddress.city", "delivery_address.city",
        "deliveryAddress.city", "delivery.city", "city", "billing_address.city", "billingAddress.city", 
        "town", "locality", "municipality", "settlement", "misto", "naselenyy_punkt", "gorod"
    ],
    "region": [
        "shipping_address.province", "shipping_address.state", "delivery_address.region",
        "deliveryAddress.region", "province", "region", "state", "billing_address.province", 
        "billingAddress.province", "territory", "county", "oblast", "area"
    ],
    "postal_code": [
        "shipping_address.zip", "shipping_address.postal_code", "delivery_address.postal_code",
        "deliveryAddress.postal_code", "zip", "postal_code", "postcode", "zip_code", 
        "zipCode", "billing_address.zip", "billingAddress.zip"
    ],
    "country": [
        "shipping_address.country", "delivery_address.country", "country_name", 
        "country", "billing_address.country", "billingAddress.country", "country_code"
    ],
    "delivery_method": [
        "delivery_method", "shipping_lines.0.title", "delivery_option.name", "delivery.title",
        "shipping_method", "shippingMethod", "carrier", "shipping_title", "shipping_description",
        "shipping_lines", "shipping_line", "delivery_service", "deliveryService", "metod_dostavky"
    ],
    "currency": [
        "currency", "presentment_currency", "currency_code", "order_currency", "currencyCode",
        "total_price_set.shop_money.currency_code", "valyuta", "val", "iso_currency_code"
    ],
    "total_amount": [
        "total_price", "current_total_price", "total_amount", "totalAmount", "total",
        "total_price_set.shop_money.amount", "price", "amount",
        "grand_total", "order_total", "sum", "subtotal", "total_sum", "suma", "v_vsogo", "itog"
    ],
    "items": [
        "line_items", "items", "products", "goods", "order_items", "lineItems", "orderlines",
        "tovary", "poziciyi", "cart_items", "contents", "order_lines", "purchased_items"
    ],
    "item_title": [
        "title", "name", "product_name", "productName", "item_name", "label", "product_title",
        "nazva", "naimenuvannia", "description", "item_description", "nazvanie_tovara"
    ],
    "item_variant": [
        "variant_title", "variant", "size", "options", "variant_name", "attribute", "properties",
        "modifikatsiya", "opcija", "parametr", "item_options", "product_options", "variation"
    ],
    "item_sku": [
        "sku", "article", "code", "product_sku", "item_code", "productSku", "itemCode",
        "artikul", "art", "product_code", "productCode", "model", "vendor_code"
    ],
    "item_quantity": [
        "quantity", "qty", "count", "amount", "pieces", "k-st", "kilkist", "kol", "item_qty",
        "product_quantity", "item_quantity", "number_of_items", "item_count", "kolichestvo"
    ],
    "item_price": [
        "price", "unit_price", "cost", "item_price", "regular_price", "unitPrice", "price_set",
        "tsina", "vartist_odynytsi", "item_cost", "product_price", "sale_price", "base_price"
    ],
    "item_line_total": [
        "line_total", "lineTotal", "total", "final_line_price", "subtotal", "row_total",
        "suma_pozytsii", "total_price", "row_sum", "item_total", "itemTotal", "line_sum"
    ],
    "item_id": [
        "id", "line_item_id", "product_id", "item_id", "variant_id", "productId", "lineItemId"
    ],
    "status": [
        "status", "fulfillment_status", "financial_status", "status_name", "order_status", 
        "fulfillmentStatus", "financialStatus", "orderStatus", "stan", "stan_zamovlennya"
    ],
    "cancelled": [
        "cancelled_at", "is_cancelled", "canceled", "cancelled", "isCancelled", "cancel_reason",
        "skasovano", "is_canceled", "is_active", "order_cancelled", "is_deleted", "deleted"
    ],
}


class OrderAPIError(Exception):
    def __init__(self, message: str, status: int = 400):
        super().__init__(message)
        self.status = status


def normalize_shop(raw: Any) -> Optional[str]:
    if not isinstance(raw, str):
        return None
    value = re.sub(r"^https?://", "", raw.strip().lower()).split("/")[0]
    return value if value else None


@lru_cache(maxsize=128)
def _cached_split_path(path: Any) -> tuple:
    if not isinstance(path, str):
        path = str(path) if path is not None else ""
    return tuple(path.split("."))


def get_path(data: Any, path: str) -> Any:
    current = data
    for part in _cached_split_path(path):
        if isinstance(current, dict):
            current = current.get(part)
        elif isinstance(current, list) and part.isdigit() and int(part) < len(current):
            current = current[int(part)]
        else:
            return None
    return None if current in EMPTY else current


def find(data: Any, field: str) -> Any:
    for path in FIELD_SYNONYMS.get(field, []):
        value = get_path(data, path)
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


def normalize_items(raw_items: Any, currency: str) -> list:
    items = []
    if not isinstance(raw_items, list):
        return items
    for item in raw_items:
        if not isinstance(item, dict):
            continue
        title = to_text(find(item, "item_title")) or "Товар"
        variant = to_text(find(item, "item_variant"))
        if variant and variant.lower() != "default title":
            title = f"{title} ({variant})"
        quantity_value = to_number(find(item, "item_quantity"))
        quantity = int(quantity_value) if quantity_value and quantity_value > 0 else 1
        unit_price = to_number(find(item, "item_price")) or 0.0
        line_total = to_number(find(item, "item_line_total"))
        items.append({
            "id": to_text(find(item, "item_id")),
            "title": title,
            "sku": to_text(find(item, "item_sku")),
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


def normalize_order(raw: dict, shop: str) -> dict:
    if not isinstance(raw, dict):
        raw = {}

    currency = to_text(find(raw, "currency")) or "UAH"

    first = to_text(find(raw, "first_name")) or to_text(get_path(raw, "customer.first_name"))
    last = to_text(find(raw, "last_name")) or to_text(get_path(raw, "customer.last_name"))
    customer_name = (
        " ".join(part for part in (first, last) if part) 
        or to_text(get_path(raw, "customer.name"))
        or to_text(find(raw, "customer_name"))
        or "Гість"
    )

    street = to_text(get_path(raw, "shipping_address.address1")) or to_text(find(raw, "street"))
    street2 = to_text(get_path(raw, "shipping_address.address2")) or to_text(find(raw, "street2"))
    full_street = " ".join(part for part in (street, street2) if part) or string_address(raw)

    city = to_text(get_path(raw, "shipping_address.city")) or to_text(find(raw, "city"))
    region = to_text(get_path(raw, "shipping_address.province")) or to_text(find(raw, "region"))
    postal_code = to_text(get_path(raw, "shipping_address.zip")) or to_text(find(raw, "postal_code"))
    country = to_text(get_path(raw, "shipping_address.country")) or to_text(find(raw, "country"))
    
    full_address = ", ".join(part for part in (country, region, city, full_street, postal_code) if part)

    phone = to_text(find(raw, "phone"))
    email = to_text(find(raw, "email"))
    chat_history = raw.get("chat_history") or raw.get("dialog_history") or ""

    base_dict = {
        "shop": shop,
        "id": to_text(get_path(raw, "id")) or to_text(find(raw, "order_number")),
        "order_number": to_text(find(raw, "order_number")),
        "created_at": to_text(find(raw, "created_at")),
        "currency": currency.upper(),
        "customer_name": customer_name,
        "phone": phone,
        "email": email,
        "delivery_method": to_text(find(raw, "delivery_method")),
        "delivery_address": {
            "street": full_street,
            "city": city,
            "region": region,
            "postal_code": postal_code,
            "country": country,
            "full": full_address,
        },
        "total_amount": money(find(raw, "total_amount"), currency),
        "items": normalize_items(find(raw, "items"), currency),
        "status": {
            "text": to_text(find(raw, "status")) or "В обробці",
            "cancelled": bool(find(raw, "cancelled")),
        },
        "chat_history": str(chat_history),
    }

    try:
        validated = UnifiedOrder(**base_dict)
        return validated.model_dump()
    except ValidationError as ve:
        logger.error(f"Pydantic Validation Error for shop {shop}: {ve.errors()}")
        return base_dict


def decrypt_token_ephemeral(encrypted_token: str) -> str:
    try:
        decrypted_bytes = cipher_suite.decrypt(encrypted_token.encode('utf-8'))
        return decrypted_bytes.decode('utf-8')
    except Exception:
        return encrypted_token


async def fetch_woocommerce_orders(shop_url: str, enc_ck: str, enc_cs: str) -> list:
    try:
        consumer_key = decrypt_token_ephemeral(enc_ck)
        consumer_secret = decrypt_token_ephemeral(enc_cs)
    except Exception:
        consumer_key, consumer_secret = enc_ck, enc_cs

    clean_url = shop_url.rstrip("/")
    if not clean_url.startswith("http"):
        clean_url = f"https://{clean_url}"
        
    url = f"{clean_url}/wp-json/wc/v3/orders"
    
    max_retries = 3
    retry_delay = 1.5

    for attempt in range(1, max_retries + 1):
        try:
            async with httpx.AsyncClient(timeout=25.0, auth=(consumer_key, consumer_secret)) as client:
                response = await client.get(url)
                
            if response.status_code == 200:
                try:
                    data = response.json()
                    return data if isinstance(data, list) else []
                except ValueError as error:
                    raise OrderAPIError(f"Магазин WooCommerce «{shop_url}» повернув невалідний JSON.", 502) from error

            if response.status_code >= 500 and attempt < max_retries:
                logger.warning(f"WooCommerce {shop_url} відповів кодом {response.status_code}. Спроба {attempt}/{max_retries}...")
                await asyncio.sleep(retry_delay)
                continue

            raise OrderAPIError(f"Помилка WooCommerce API для «{shop_url}»: код {response.status_code}.", response.status_code)

        except (httpx.TimeoutException, httpx.RequestError) as error:
            if attempt < max_retries:
                logger.warning(f"Мережевий збій з WooCommerce {shop_url} (спроба {attempt}/{max_retries}). Повторюємо...")
                await asyncio.sleep(retry_delay)
                continue
            if isinstance(error, httpx.TimeoutException):
                raise OrderAPIError(f"Час очікування запиту до WooCommerce «{shop_url}» вичерпано.", 504) from error
            raise OrderAPIError(f"Не вдалося підключитися до WooCommerce «{shop_url}».", 502) from error


async def fetch_orders_multi(stores: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    if not isinstance(stores, list) or not stores:
        return []

    async def fetch_single_store(store_info: Dict[str, Any]) -> List[Dict[str, Any]]:
        platform = store_info.get("platform", "woocommerce").lower()
        shop_raw = store_info.get("shop") or store_info.get("shop_domain")
        shop = normalize_shop(shop_raw) or shop_raw
        credentials = store_info.get("credentials", {})
        
        try:
            raw_orders = []
            if platform == "woocommerce":
                ck = credentials.get("consumer_key", "")
                cs = credentials.get("consumer_secret", "")
                raw_orders = await fetch_woocommerce_orders(shop, ck, cs)
            else:
                ck = credentials.get("consumer_key", "")
                cs = credentials.get("consumer_secret", "")
                raw_orders = await fetch_woocommerce_orders(shop, ck, cs)

            return [normalize_order(raw_ord, shop) for raw_ord in raw_orders if isinstance(raw_ord, dict)]

        except Exception as e:
            logger.error(f"Failed to fetch orders for store {shop}: {str(e)}")
            return []

    tasks = [fetch_single_store(store) for store in stores]
    results = await asyncio.gather(*tasks, return_exceptions=True)
    
    all_orders = []
    for res in results:
        if isinstance(res, list):
            all_orders.extend(res)
            
    return all_orders