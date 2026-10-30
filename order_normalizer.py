"""Універсальний обробник замовлень для будь-якого магазину.

Не містить логіки під конкретний сайт. Сире замовлення з будь-якого API
приводиться до одного формату через пошук полів за синонімами, у тому числі
у вкладених об'єктах (customer.first_name, buyer.name, shipping_address.city тощо).
"""
import re
from typing import Any, Optional

import requests

# Синоніми полів: перший знайдений у замовленні виграє. Шлях може бути вкладеним через крапку.
FIELD_SYNONYMS = {
    "order_number": ["order_number", "orderNumber", "number", "name", "order_id", "id"],
    "created_at": ["created_at", "createdAt", "date_created", "date", "created"],
    "customer_name": ["customer_name", "customerName", "client_name", "buyer_name", "full_name",
                      "customer.full_name", "customer.fullName", "customer.name", "buyer.full_name",
                      "buyer.name", "client.name", "client_name", "recipient_name", "shipping_address.name", "shippingAddress.name"],
    "first_name": ["customer.first_name", "customer.firstName", "buyer.first_name", "client.first_name",
                   "shipping_address.first_name", "shippingAddress.first_name", "billing_address.first_name",
                   "client_first_name", "first_name", "firstName"],
    "last_name": ["customer.last_name", "customer.lastName", "buyer.last_name", "client.last_name",
                  "shipping_address.last_name", "shippingAddress.last_name", "billing_address.last_name",
                  "client_last_name", "last_name", "lastName"],
    "phone": ["shipping_address.phone", "shippingAddress.phone", "delivery_address.phone",
              "deliveryAddress.phone", "customer.phone", "buyer.phone", "client.phone", "phone",
              "billing_address.phone", "delivery.phone", "telephone", "phone_number", "contact_phone", "client_phone"],
    "email": ["email", "customer.email", "buyer.email", "client.email", "shipping_address.email", "client_email"],
    "street": ["shipping_address.address1", "shippingAddress.address1", "delivery_address.street",
               "deliveryAddress.street", "delivery_address.address1", "deliveryAddress.address1",
               "delivery.address", "address1", "address", "street"],
    "street2": ["shipping_address.address2", "shippingAddress.address2", "delivery_address.address2",
                "deliveryAddress.address2", "address2", "apartment"],
    "city": ["shipping_address.city", "shippingAddress.city", "delivery_address.city",
             "deliveryAddress.city", "delivery.city", "city"],
    "region": ["shipping_address.province", "shippingAddress.province", "shipping_address.state",
               "shippingAddress.state", "delivery_address.region", "deliveryAddress.region",
               "province", "region", "state"],
    "postal_code": ["shipping_address.zip", "shippingAddress.zip", "shipping_address.postal_code",
                    "shippingAddress.postal_code", "delivery_address.postal_code", "deliveryAddress.postal_code",
                    "zip", "postal_code", "postcode"],
    "country": ["shipping_address.country", "shippingAddress.country", "shipping_address.country_name",
                "shippingAddress.country_name", "delivery_address.country", "deliveryAddress.country",
                "country_name", "country"],
    "delivery_method": ["delivery_method", "shipping_lines.0.title", "delivery_option.name", "delivery.title"],
    "currency": ["currency", "presentment_currency", "currency_code",
                 "total_price_set.shop_money.currency_code", "total_price_set.presentment_money.currency_code"],
    "total_amount": ["total_price", "current_total_price", "total_amount", "totalAmount", "total",
                     "total_price_set.shop_money.amount", "total_price_set.presentment_money.amount", "price", "amount"],
    "items": ["line_items", "items", "products", "goods"],
    "item_title": ["title", "name", "product_name"],
    "item_variant": ["variant_title", "variant", "size"],
    "item_sku": ["sku", "article", "code"],
    "item_quantity": ["quantity", "qty", "count"],
    "item_price": ["price", "unit_price", "cost"],
    "item_line_total": ["line_total", "lineTotal", "total", "final_line_price"],
    "item_id": ["id", "line_item_id"],
    "status": ["status", "fulfillment_status", "financial_status", "status_name"],
    "cancelled": ["cancelled_at", "is_cancelled"],
}

CURRENCY_PREFIX = {"USD": "$", "EUR": "€", "GBP": "£"}
EMPTY = (None, "", [], {})


class OrderAPIError(Exception):
    """Помилка роботи з API магазину; status — HTTP-код для відповіді клієнту."""

    def __init__(self, message: str, status: int = 400):
        super().__init__(message)
        self.status = status


def normalize_shop(raw: Any) -> Optional[str]:
    """'https://My-Store.myshopify.com/' -> 'my-store.myshopify.com'; 'my-store' -> 'my-store.myshopify.com'."""
    if not isinstance(raw, str):
        return None
    value = re.sub(r"^https?://", "", raw.strip().lower()).split("/")[0]
    if not value:
        return None
    # Shopify-домен без ".myshopify.com" (наприклад, лише назва магазину) не резолвиться в DNS
    if "." not in value:
        value = f"{value}.myshopify.com"
    return value


def get_path(data: Any, path: str) -> Any:
    """Дістає значення за шляхом 'a.b.0.c'; повертає None, якщо шляху немає."""
    current = data
    for part in path.split("."):
        if isinstance(current, dict):
            current = current.get(part)
        elif isinstance(current, list) and part.isdigit() and int(part) < len(current):
            current = current[int(part)]
        else:
            return None
    return None if current in EMPTY else current


def find(data: Any, field: str) -> Any:
    """Перше непорожнє значення за синонімами поля."""
    for path in FIELD_SYNONYMS[field]:
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
    """Адреса, що прийшла одним рядком (наприклад, delivery_address: 'вул. Центральна, 15')."""
    if not isinstance(raw, dict):
        return ""
    for key in ("delivery_address", "deliveryAddress", "shipping_address", "shippingAddress", "address"):
        value = raw.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return ""


def deep_find(data: Any, hints: tuple) -> str:
    """Рекурсивно шукає непорожнє значення, ключ якого містить одну з підказок (наприклад, 'phone')."""
    if isinstance(data, dict):
        for key, value in data.items():
            if any(hint in str(key).lower() for hint in hints) and to_text(value):
                return to_text(value)
        candidates = list(data.values())
    elif isinstance(data, list):
        candidates = data
    else:
        return ""
    for value in candidates:
        if isinstance(value, (dict, list)):
            found = deep_find(value, hints)
            if found:
                return found
    return ""


def normalize_order(raw: dict, shop: str) -> dict:
    """Стандартне замовлення незалежно від назв полів у сирому JSON."""
    currency = to_text(find(raw, "currency")) or "UAH"

    first = to_text(find(raw, "first_name"))
    last = to_text(find(raw, "last_name"))
    customer_name = " ".join(part for part in (first, last) if part) or to_text(find(raw, "customer_name"))

    street = " ".join(part for part in (to_text(find(raw, "street")), to_text(find(raw, "street2"))) if part)
    street = street or string_address(raw)
    city = to_text(find(raw, "city"))
    region = to_text(find(raw, "region"))
    postal_code = to_text(find(raw, "postal_code"))
    country = to_text(find(raw, "country"))
    # Порядок: країна, область, місто, вулиця/будинок, індекс
    full_address = ", ".join(part for part in (country, region, city, street, postal_code) if part)

    return {
        "shop": shop,
        "id": to_text(get_path(raw, "id")) or to_text(find(raw, "order_number")),
        "order_number": to_text(find(raw, "order_number")),
        "created_at": to_text(find(raw, "created_at")),
        "currency": currency.upper(),
        "customer_name": customer_name,
        "phone": to_text(find(raw, "phone")) or deep_find(raw, ("phone",)),
        "email": to_text(find(raw, "email")) or deep_find(raw, ("email",)),
        "delivery_method": to_text(find(raw, "delivery_method")),
        "delivery_address": {
            "street": street,
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
    }




def shopify_error(status_code: int, shop: str) -> OrderAPIError:
    """Зрозуміле повідомлення для HTTP-відповіді Shopify; статус віддаємо клієнту."""
    if status_code == 401:
        return OrderAPIError(
            f"Невірний або недійсний API-токен для магазину «{shop}». Оновіть токен в адмін-панелі Shopify.", 401
        )
    if status_code == 403:
        return OrderAPIError(
            f"Токен для магазину «{shop}» не має необхідних прав: увімкніть доступ read_orders "
            "(Settings → Apps → Admin API) і згенеруйте токен заново.", 403
        )
    if status_code == 404:
        return OrderAPIError(f"Магазин «{shop}» не знайдено. Перевірте назву магазину.", 404)
    if status_code == 429:
        return OrderAPIError(f"Магазин «{shop}» тимчасово обмежив запити. Спробуйте пізніше.", 429)
    return OrderAPIError(f"Помилка API магазину «{shop}»: код {status_code}.", 502)


def fetch_orders(shop: str, token: str, orders_url: Optional[str] = None) -> list:
    """Завантажує сирі замовлення з API магазину.

    Якщо orders_url не задано, використовується Shopify-сумісний шлях
    https://<shop>/admin/api/2024-01/orders.json. Для інших сайтів передайте orders_url.
    """
    # 2024-01 більше не підтримується Shopify (запити переадресовуються на старішу версію), тому оновлено
    url = orders_url or f"https://{shop}/admin/api/2026-10/orders.json?status=any"
    # Shopify вимагає X-Shopify-Access-Token; Bearer лишаємо для сумісності з іншими API
    headers = {"X-Shopify-Access-Token": token, "Authorization": f"Bearer {token}", "Accept": "application/json"}
    print(f"[DEBUG] Запит до магазину: GET {url}", flush=True)
    try:
        response = requests.get(url, headers=headers, timeout=25)
    except requests.RequestException as error:
        print(f"[DEBUG] Помилка мережі до {url}: {type(error).__name__}: {error}", flush=True)
        message = (
            f"Не вдалося підключитися до магазину «{shop}». Перевірте назву магазину "
            "(наприклад, my-store або my-store.myshopify.com) і з'єднання з інтернетом."
        )
        raise OrderAPIError(message, 502) from error

    if response.status_code != 200:
        # Точний текст відповіді Shopify: видно, чи причина в токені, URL або правах
        print(f"[DEBUG] Shopify відповів {response.status_code} для {url}: {response.text}", flush=True)
        raise shopify_error(response.status_code, shop)

    data = response.json()
    if isinstance(data, list):
        return data
    return data.get("orders") or data.get("data") or data.get("items") or []
