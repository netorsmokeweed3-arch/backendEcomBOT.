import json
import os

import anthropic

# Зчитуємо API-ключ зі змінних оточення сервера
ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY")
client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)

MODEL = "claude-haiku-5-5"

SYSTEM_PROMPT = """Ти — система обробки замовлень. Твоя відповідь — ЛИШЕ сирий JSON-об'єкт.
Заборонено: markdown, коментарі, пояснення, поради, текст до або після JSON.
Пам'ятай, що різні платформи (Shopify, Prom, Розетка тощо) надсилають дані у різних форматах та з різними назвами ключів.
Шукай ім'я (customer_name/name), телефон (phone/telephone) та повну адресу (delivery_address/shipping_address)
у будь-яких можливих вкладених структурах вхідного JSON замовлення і завжди повертай їх у єдиному стандартному форматі.
Ніколи не придумуй значення: якщо поле відсутнє у всіх структурах замовлення, поверни для нього порожній рядок.
Витягни дані саме із структурованого замовлення. Воно може містити поля Shopify
(customer, shipping_address, line_items, total_price), вже нормалізовані поля або їх
camelCase-варіанти. Для телефону перевір адресу доставки, customer/buyer і саме замовлення.
Для адреси об'єднай вулицю, квартиру, місто, область/штат, поштовий індекс і країну.
Для кожного товару поверни назву, кількість і суму рядка; використовуй передану суму рядка,
а якщо її немає — ціну за одиницю та кількість. Суми і валюти копіюй із замовлення.
Історія чату не є джерелом фактів замовлення. Не виводь і не припускай відсутні значення.
Схема (усі ключі обов'язкові, типи строгі):
{
  "customer_name": string,
  "phone": string,
  "email": string,
  "delivery_address": string,
  "city": string,
  "region": string,
  "postal_code": string,
  "country": string,
  "delivery_method": string,
  "total_amount": string,
  "currency": string,
  "order_number": string,
  "items": [{"title": string, "quantity": number, "line_total": string}],
  "ai_summary": string
}
ai_summary: один короткий опис наявного статусу, 1–2 речення, без порад.
Якщо значення немає у вхідному замовленні, використовуй порожній рядок або [] (кількість — 0)."""


def build_user_message(order: dict, chat_messages: str) -> str:
    return (
        "Структуровані дані замовлення (головне джерело фактів):\n"
        f"{json.dumps(order, ensure_ascii=False, indent=2)}\n\n"
        "Історія чату (лише контекст; не використовуй для заповнення полів замовлення):\n"
        f"{chat_messages or '—'}"
    )


def parse_json_object(text: str) -> dict:
    """Витягує перший JSON-об'єкт з відповіді; якщо його немає, кидає ValueError."""
    start, end = text.find("{"), text.rfind("}")
    if start == -1 or end == -1:
        raise ValueError("У відповіді ШІ немає JSON-об'єкта.")
    parsed = json.loads(text[start : end + 1])
    if not isinstance(parsed, dict):
        raise ValueError("JSON від ШІ не є об'єктом.")
    return parsed


def standard_fields(order: dict) -> dict:
    """Факти беремо з нормалізованого замовлення, а не з відповіді моделі."""
    address = order.get("delivery_address") or {}
    total = order.get("total_amount") or {}
    if not isinstance(address, dict):
        address = {}
    if not isinstance(total, dict):
        total = {}
    source_items = order.get("items")
    if not isinstance(source_items, list):
        source_items = []
    return {
        "customer_name": order.get("customer_name") or "",
        "phone": order.get("phone") or "",
        "email": order.get("email") or "",
        "delivery_address": address.get("full") or "",
        "city": address.get("city") or "",
        "region": address.get("region") or "",
        "postal_code": address.get("postal_code") or "",
        "country": address.get("country") or "",
        "delivery_method": order.get("delivery_method") or "",
        "total_amount": total.get("formatted") or "",
        "currency": total.get("currency") or "",
        "order_number": order.get("order_number") or "",
        "items": [
            {
                "title": item.get("title", ""),
                "quantity": item.get("quantity", 1),
                "line_total": (item.get("line_total") or {}).get("formatted", ""),
                "sku": item.get("sku", ""),
            }
            for item in source_items
            if isinstance(item, dict)
        ],
        "ai_summary": "",
    }


def fill_missing_fields(report: dict, extracted: dict) -> None:
    """Use Claude extraction only as a fallback for fields absent from normalized source data."""
    for field in (
        "customer_name", "phone", "email", "delivery_address", "city", "region",
        "postal_code", "country", "delivery_method", "total_amount", "currency", "order_number",
    ):
        value = extracted.get(field)
        if not report.get(field) and isinstance(value, str):
            report[field] = value.strip()

    if not report["items"] and isinstance(extracted.get("items"), list):
        items = []
        for item in extracted["items"]:
            if not isinstance(item, dict):
                continue
            title = item.get("title")
            quantity = item.get("quantity")
            line_total = item.get("line_total")
            if (
                isinstance(title, str)
                and isinstance(quantity, (int, float))
                and not isinstance(quantity, bool)
                and isinstance(line_total, str)
            ):
                items.append({
                    "title": title.strip(),
                    "quantity": quantity,
                    "line_total": line_total.strip(),
                    "sku": "",
                })
        if items:
            report["items"] = items


def analyze_order(order: dict, chat_messages: str) -> dict:
    """Повертає стандартний звіт: факти з замовлення + короткий ai_summary від Claude."""
    report = standard_fields(order)

    try:
        response = client.messages.create(
            model=MODEL,
            max_tokens=900,
            system=SYSTEM_PROMPT,
            messages=[{"role": "user", "content": build_user_message(order, chat_messages)}],
        )
        response_text = "".join(block.text for block in response.content if hasattr(block, "text"))
        extracted = parse_json_object(response_text)
        fill_missing_fields(report, extracted)
        summary = extracted.get("ai_summary")
        report["ai_summary"] = summary.strip() if isinstance(summary, str) else ""
    except (ValueError, json.JSONDecodeError) as error:
        print(f"Claude повернув некоректний JSON: {error}")
        report["ai_summary"] = "ШІ не зміг сформувати статус. Перевірте замовлення вручну."
    except Exception as error:
        print(f"Помилка при зверненні до Claude API: {error}")
        report["ai_summary"] = "ШІ тимчасово недоступний. Спробуйте пізніше."

    return report
