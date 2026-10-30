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
    "order_number": [
        "order_number", "orderNumber", "number", "name", "order_id", "id", 
        "order_no", "orderNo", "reference", "code", "invoice_no", "invoiceNumber",
        "order_reference", "external_id", "externalId", "display_number", "displayNumber",
        "order_code", "orderCode", "increment_id", "incrementId", "num", "order_num",
        "zakaz_id", "nomer_zakazu", "nomer", "transaction_id", "transactionId", "order_index",
        "order_identifier", "orderIdentifier", "doc_number", "document_number", "sales_order_id"
    ],
    "created_at": [
        "created_at", "createdAt", "date_created", "date", "created", 
        "order_date", "date_added", "timestamp", "inserted_at", "processed_at",
        "processedAt", "date_modified", "updated_at", "updatedAt", "date_purchased",
        "created_time", "createdTime", "order_time", "orderTime", "datetime",
        "date_added_to_db", "time", "order_timestamp", "orderTimestamp", "date_placed",
        "creation_date", "creationDate", "placed_at", "placedAt", "order_date_added"
    ],
    "customer_name": [
        "customer.full_name", "customer.fullName", "customer.name", "customer_name", "customerName",
        "client_name", "buyer_name", "full_name", "fullName", "buyer.full_name",
        "buyer.name", "client.name", "recipient_name", "shipping_address.name", "shippingAddress.name",
        "billing_address.name", "billingAddress.name", "shipping_name", "billing_name",
        "contact_name", "contactName", "receiver", "recipient", "customer_fullname",
        "client_fullname", "buyer_fullname", "pib", "fio", "name_surname", "client_fio",
        "customer_fio", "buyer_fio", "imya_prizvyshche", "osoba", "kontragent",
        "customer.display_name", "buyer.display_name", "customer.username", "customerNameSurname",
        "client_full_name", "buyer_full_name", "customer_details.name", "user_name", "userName"
    ],
    "first_name": [
        "customer.first_name", "customer.firstName", "buyer.first_name", "client.first_name",
        "shipping_address.first_name", "shippingAddress.first_name", "billing_address.first_name", "billingAddress.first_name",
        "client_first_name", "first_name", "firstName", "shipping_firstname", "billing_firstname",
        "fname", "given_name", "givenName", "name", "imya", "customer_firstname", "buyer_firstname",
        "customer.fname", "buyer.fname", "shipping_address.fname", "billing_address.fname", "client_fname"
    ],
    "last_name": [
        "customer.last_name", "customer.lastName", "buyer.last_name", "client.last_name",
        "shipping_address.last_name", "shippingAddress.last_name", "billing_address.last_name", "billingAddress.last_name",
        "client_last_name", "last_name", "lastName", "shipping_lastname", "billing_lastname",
        "lname", "surname", "family_name", "familyName", "prizvyshche", "customer_lastname", "buyer_lastname",
        "customer.lname", "buyer.lname", "shipping_address.lname", "billing_address.lname", "client_lname"
    ],
    "phone": [
        "shipping_address.phone", "shippingAddress.phone", "delivery_address.phone",
        "deliveryAddress.phone", "customer.phone", "buyer.phone", "client.phone", "phone",
        "billing_address.phone", "billingAddress.phone", "delivery.phone", "telephone", 
        "phone_number", "phoneNumber", "contact_phone", "client_phone", "mobile", "cellphone",
        "customer_phone", "contact_number", "contactNumber", "tel", "cell", "customer.phone_number",
        "shipping_phone", "billing_phone", "telefon", "nomer_telefonu", "mobilnyi", "mob_phone",
        "customer.telephone", "buyer.phone_number", "recipient_phone", "customer_mobile",
        "billing_address.telephone", "shipping_address.telephone", "contact_info.phone"
    ],
    "email": [
        "email", "customer.email", "buyer.email", "client.email", "shipping_address.email", 
        "shippingAddress.email", "billing_address.email", "billingAddress.email", "client_email", 
        "customer_email", "mail", "contact_email", "contactEmail", "customer.mail", "e_mail",
        "elektronna_poshta", "pochta", "buyer.email_address", "customer.email_address",
        "shipping_address.customer_email", "user_email", "userEmail", "contact_info.email"
    ],
    "street": [
        "shipping_address.address1", "shippingAddress.address1", "delivery_address.street",
        "deliveryAddress.street", "delivery_address.address1", "deliveryAddress.address1",
        "delivery.address", "address1", "address", "street", "shipping_address.street", 
        "billing_address.address1", "billingAddress.address1", "line1", "street_address", "streetAddress",
        "shipping_address.address", "shippingAddress.address", "delivery_address.address", "deliveryAddress.address",
        "street_name", "streetName", "delivery_street", "vulytsia", "vul", "street1",
        "shipping_address.street_address", "delivery_address.line1", "address_1", "addressLine1",
        "shipping_address.address_1", "billing_address.address_1", "street1_address"
    ],
    "street2": [
        "shipping_address.address2", "shippingAddress.address2", "delivery_address.address2",
        "deliveryAddress.address2", "address2", "apartment", "apt", "suite", "unit", 
        "billing_address.address2", "billingAddress.address2", "line2", "building", "house", "room",
        "apartment_number", "apartmentNumber", "flat", "kv", "bud", "korпус", "building_number",
        "dom", "kvartira", "ofis", "office", "shipping_address.apartment", "delivery_address.apartment",
        "address_2", "addressLine2", "shipping_address.address_2", "billing_address.address_2",
        "house_number", "houseNumber", "building_no", "flat_no"
    ],
    "city": [
        "shipping_address.city", "shippingAddress.city", "delivery_address.city",
        "deliveryAddress.city", "delivery.city", "city", "billing_address.city", "billingAddress.city", 
        "town", "locality", "municipality", "settlement", "misto", "naselenyy_punkt", "gorod",
        "shipping_address.town", "delivery_address.city_name", "billing_address.town", "city_name"
    ],
    "region": [
        "shipping_address.province", "shippingAddress.province", "shipping_address.state",
        "shippingAddress.state", "delivery_address.region", "deliveryAddress.region",
        "province", "region", "state", "billing_address.province", "billingAddress.province", 
        "billing_address.state", "billingAddress.state", "territory", "county", "oblast", "area",
        "shtat", "reys", "rayon", "shipping_address.region_name", "state_province", "stateProvince"
    ],
    "postal_code": [
        "shipping_address.zip", "shippingAddress.zip", "shipping_address.postal_code",
        "shippingAddress.postal_code", "delivery_address.postal_code", "deliveryAddress.postal_code",
        "zip", "postal_code", "postcode", "zip_code", "zipCode", "billing_address.zip", "billingAddress.zip",
        "index", "post_index", "poshtovyi_indeks", "shipping_address.postcode", "billing_address.postal_code"
    ],
    "country": [
        "shipping_address.country", "shippingAddress.country", "shipping_address.country_name",
        "shippingAddress.country_name", "delivery_address.country", "deliveryAddress.country",
        "country_name", "country", "billing_address.country", "billingAddress.country", "country_code", "countryCode",
        "krayina", "strana", "shipping_address.country_code", "billing_address.country_code"
    ],
    "delivery_method": [
        "delivery_method", "shipping_lines.0.title", "delivery_option.name", "delivery.title",
        "shipping_method", "shippingMethod", "carrier", "shipping_title", "shipping_description",
        "shipping_lines", "shipping_line", "delivery_service", "deliveryService", "metod_dostavky",
        "sposib_dostavky", "transport", "shipping_carrier", "shipping_method_title", "shipping_type"
    ],
    "currency": [
        "currency", "presentment_currency", "currency_code", "order_currency", "currencyCode",
        "total_price_set.shop_money.currency_code", "total_price_set.presentment_money.currency_code",
        "shop_money.currency_code", "valyuta", "val", "iso_currency_code", "order_currency_code"
    ],
    "total_amount": [
        "total_price", "current_total_price", "total_amount", "totalAmount", "total",
        "total_price_set.shop_money.amount", "total_price_set.presentment_money.amount", "price", "amount",
        "grand_total", "order_total", "sum", "subtotal", "total_sum", "totalSum", "total_price_usd",
        "suma", "v_vsogo", "itog", "total_price_set.shop_money.shop_amount", "final_amount",
        "total_paid", "order_amount", "summa", "total_price_decimal"
    ],
    "items": [
        "line_items", "items", "products", "goods", "order_items", "lineItems", "orderlines", "products_list",
        "tovary", "poziciyi", "cart_items", "contents", "order_lines", "purchased_items"
    ],
    "item_title": [
        "title", "name", "product_name", "productName", "item_name", "label", "product_title",
        "nazva", "naimenuvannia", "product_label", "item_label", "product_nicename"
    ],
    "item_variant": [
        "variant_title", "variant", "size", "options", "variant_name", "attribute", "properties", "variantTitle",
        "modifikatsiya", "opcija", "parametr", "item_options", "product_options", "variation"
    ],
    "item_sku": [
        "sku", "article", "code", "product_sku", "item_code", "productSku", "itemCode",
        "artikul", "art", "product_code", "productCode"
    ],
    "item_quantity": [
        "quantity", "qty", "count", "amount", "pieces", "k-st", "kilkist", "kol", "item_qty",
        "product_quantity", "item_quantity"
    ],
    "item_price": [
        "price", "unit_price", "cost", "item_price", "regular_price", "unitPrice", "price_set",
        "tsina", "vartist_odynytsi", "item_cost", "product_price"
    ],
    "item_line_total": [
        "line_total", "lineTotal", "total", "final_line_price", "subtotal", "row_total", "total_discount",
        "suma_pozytsii", "total_price", "row_sum", "item_total"
    ],
    "item_id": [
        "id", "line_item_id", "product_id", "item_id", "variant_id", "productId", "lineItemId",
        "item_identifier"
    ],
    "status": [
        "status", "fulfillment_status", "financial_status", "status_name", "order_status", 
        "fulfillmentStatus", "financialStatus", "orderStatus", "stan", "stan_zamovlennya",
        "order_state", "state", "payment_status", "paymentStatus"
    ],
    "cancelled": [
        "cancelled_at", "is_cancelled", "canceled", "cancelled", "isCancelled", "cancel_reason",
        "skasovano", "is_canceled", "is_active", "order_cancelled", "cancelled_status"
    ],
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
    """Адреса, що прийшла одним рядком."""
    if not isinstance(raw, dict):
        return ""
    for key in ("delivery_address", "deliveryAddress", "shipping_address", "shippingAddress", "address"):
        value = raw.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return ""


def deep_find(data: Any, hints: tuple) -> str:
    """Рекурсивно шукає непорожнє значення за підказками."""
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

    # Гарантоване збирання імені з customer або з окремих полів першого/останнього імені
    first = to_text(find(raw, "first_name")) or to_text(get_path(raw, "customer.first_name"))
    last = to_text(find(raw, "last_name")) or to_text(get_path(raw, "customer.last_name"))
    customer_name = " ".join(part for part in (first, last) if part) or to_text(find(raw, "customer_name"))

    street = " ".join(part for part in (to_text(find(raw, "street")), to_text(find(raw, "street2"))) if part)
    street = street or string_address(raw)
    city = to_text(find(raw, "city"))
    region = to_text(find(raw, "region"))
    postal_code = to_text(find(raw, "postal_code"))
    country = to_text(find(raw, "country"))
    
    full_address = ", ".join(part for part in (country, region, city, street, postal_code) if part)

    # Гарантоване витягування телефону з урахуванням Shopify-структури (shipping_address або customer)
    phone = (
        to_text(find(raw, "phone")) 
        or to_text(get_path(raw, "shipping_address.phone")) 
        or to_text(get_path(raw, "customer.phone"))
        or deep_find(raw, ("phone",))
    )

    email = (
        to_text(find(raw, "email"))
        or to_text(get_path(raw, "customer.email"))
        or deep_find(raw, ("email",))
    )

    return {
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
    url = orders_url or f"https://{shop}/admin/api/2026-10/orders.json?status=any"
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
        print(f"[DEBUG] Shopify відповів {response.status_code} для {url}: {response.text}", flush=True)
        raise shopify_error(response.status_code, shop)

    data = response.json()
    if isinstance(data, list):
        return data
    return data.get("orders") or data.get("data") or data.get("items") or []