import re
from typing import Any, Dict, Optional, List

def normalize_shop(shop_url: Optional[str]) -> Optional[str]:
    """Очищує та нормалізує домен магазину."""
    if not shop_url or not isinstance(shop_url, str):
        return None
    cleaned = shop_url.strip().lower()
    cleaned = re.sub(r"^https?://", "", cleaned)
    cleaned = cleaned.split("/")[0]
    return cleaned if cleaned else None

def to_number(value: Any) -> Optional[float | int]:
    """Безпечно конвертує вхідне значення у float/int, очищаючи пробіли та замінюючи кому на крапку."""
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        cleaned = value.strip().replace(" ", "").replace(",", ".")
        cleaned = re.sub(r"[^\d.-]", "", cleaned)
        if not cleaned:
            return None
        try:
            val = float(cleaned)
            return int(val) if val.is_integer() else val
        except ValueError:
            return None
    return None

def money(amount: Any, currency: str = "UAH") -> Dict[str, Any]:
    """Форматує грошову суму та валюту."""
    num = to_number(amount) or 0.0
    return {
        "amount": num,
        "currency": currency.upper(),
        "formatted": f"{num:.2f} {currency.upper()}"
    }

def normalize_order(raw_order: Dict[str, Any], shop_domain: str) -> Dict[str, Any]:
    """Нормалізує сирий об'єкт замовлення до єдиної внутрішньої структури."""
    safe_order = raw_order if isinstance(raw_order, dict) else {}
    
    order_number = str(safe_order.get("order_id") or safe_order.get("id") or "")
    total_price = to_number(safe_order.get("total_price") or safe_order.get("total") or 0) or 0.0
    currency = str(safe_order.get("currency") or "UAH")
    
    customer = safe_order.get("customer") or {}
    if isinstance(customer, dict):
        first_name = str(customer.get("first_name", ""))
        last_name = str(customer.get("last_name", ""))
        customer_name = f"{first_name} {last_name}".strip()
        email = str(customer.get("email", ""))
    else:
        customer_name = ""
        email = ""

    line_items = safe_order.get("line_items") or safe_order.get("items") or []
    formatted_items = []
    if isinstance(line_items, list):
        for item in line_items:
            if not isinstance(item, dict):
                continue
            title = str(item.get("name") or item.get("title", ""))
            qty = to_number(item.get("quantity") or item.get("qty") or 1) or 1
            price = to_number(item.get("price") or item.get("unit_price") or 0) or 0.0
            sku = str(item.get("sku", ""))
            
            formatted_items.append({
                "title": title,
                "quantity": qty,
                "price": price,
                "sku": sku
            })

    return {
        "shop": normalize_shop(shop_domain),
        "order_number": order_number,
        "customer_name": customer_name,
        "email": email,
        "total_amount": money(total_price, currency),
        "items": formatted_items
    }