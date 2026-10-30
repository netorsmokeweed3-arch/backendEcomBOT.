import json
import os
import anthropic

# Зчитуємо API-ключ зі змінних оточення сервера
ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY")
if not ANTHROPIC_API_KEY:
    print("[WARNING] ANTHROPIC_API_KEY не задано у змінних оточення!")

client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)

MODEL = "claude-haiku-5-5"
MAX_RESPONSE_LENGTH = 15000  # Захист від завеликих відповідей

SYSTEM_PROMPT = """Ти — система обробки замовлень. Твоя відповідь — ЛИШЕ сирий JSON-об'єкт.
Заборонено: markdown, коментарі, пояснення, поради, текст до або після JSON.
Пам'ятай, що різні платформи (Shopify, Prom, Розетка тощо) надсилають дані у різних форматах та з різними назвами ключів.
Шукай ім'я (customer_name/name), телефон (phone/telephone), повну адресу (delivery_address), статус (status), 
помітки (note/comments), платіжний шлюз (payment_gateway) та товари у будь-яких структурах вхідного JSON.
Ніколи не придумуй значення: якщо поле відсутнє у всіх структурах замовлення, поверни для нього порожній рядок або [].
Для кожного товару поверни назву (title), кількість (quantity), суму рядка (line_total), артикул (sku) та ID.
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
  "status": string,
  "cancelled": boolean,
  "note": string,
  "payment_gateway": string,
  "items": [
    {
      "title": string,
      "quantity": number,
      "line_total": string,
      "sku": string,
      "item_id": string
    }
  ],
  "ai_summary": string
}
ai_summary: один короткий опис наявного статусу, 1–2 речення, без порад."""


def build_user_message(order: dict, chat_messages: str) -> str:
    safe_order = order if isinstance(order, dict) else {}
    safe_chat = str(chat_messages or "—")
    return (
        "Структуровані дані замовлення (головне джерело фактів):\n"
        f"{json.dumps(safe_order, ensure_ascii=False, indent=2)}\n\n"
        "Історія чату (лише контекст; не використовуй для заповнення полів замовлення):\n"
        f"{safe_chat}"
    )


def parse_json_object(text: str) -> dict:
    """Безпечно витягує перший JSON-об'єкт з перевіркою довжини та типів."""
    if not isinstance(text, str) or not text.strip():
        raise ValueError("Отримано порожню відповідь від ШІ.")
    
    if len(text) > MAX_RESPONSE_LENGTH:
        raise ValueError("Відповідь ШІ перевищує допустимий ліміт довжини.")

    start, end = text.find("{"), text.rfind("}")
    if start == -1 or end == -1:
        raise ValueError("У відповіді ШІ немає JSON-об'єкта.")
    
    parsed = json.loads(text[start : end + 1])
    if not isinstance(parsed, dict):
        raise ValueError("JSON від ШІ не є об'єктом.")
    return parsed


def standard_fields(order: dict) -> dict:
    """Факти беремо з нормалізованого замовлення, гарантуючи захист від невалідних типів."""
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

    return {
        "customer_name": str(safe_order.get("customer_name") or safe_order.get("name", "")),
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
    }


def fill_missing_fields(report: dict, extracted: dict) -> None:
    """Суворо перевірене заповнення пропущених полів із сирої відповіді Claude."""
    if not isinstance(extracted, dict):
        return

    for field in (
        "customer_name", "phone", "email", "delivery_address", "city", "region",
        "postal_code", "country", "delivery_method", "total_amount", "currency", 
        "order_number", "status", "note", "payment_gateway"
    ):
        value = extracted.get(field)
        if not report.get(field) and isinstance(value, str) and value.strip():
            report[field] = value.strip()

    if isinstance(extracted.get("cancelled"), bool):
        report["cancelled"] = extracted.get("cancelled")

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


def analyze_order(order: dict, chat_messages: str) -> dict:
    """Головна точка входу: безпечний аналіз та збирання замовлення."""
    report = standard_fields(order)

    try:
        response = client.messages.create(
            model=MODEL,
            max_tokens=1000,
            system=SYSTEM_PROMPT,
            messages=[{"role": "user", "content": build_user_message(order, chat_messages)}],
        )
        response_text = "".join(block.text for block in response.content if hasattr(block, "text"))
        extracted = parse_json_object(response_text)
        fill_missing_fields(report, extracted)
        summary = extracted.get("ai_summary")
        report["ai_summary"] = summary.strip() if isinstance(summary, str) else ""
    except (ValueError, json.JSONDecodeError) as error:
        print(f"[ERROR] Помилка обробки відповіді Claude: {error}")
        report["ai_summary"] = "ШІ не зміг сформувати статус. Перевірте замовлення вручну."
    except Exception as error:
        print(f"[ERROR] Критична помилка при зверненні до Claude API: {error}")
        report["ai_summary"] = "ШІ тимчасово недоступний. Спробуйте пізніше."

    return report