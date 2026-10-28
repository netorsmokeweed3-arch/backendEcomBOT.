import os
import json
import anthropic

# Зчитуємо API-ключ зі змінних оточення сервера
ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY")
client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)

EMPTY_REPORT = {
    "customerName": "",
    "phone": "",
    "city": "",
    "postOffice": "",
    "products": [],
    "orderStatus": "",
    "chatAnalysis": "",
    "recommendations": [],
}


def build_prompt(shop, order_data, chat_messages):
    order_json = json.dumps(order_data, ensure_ascii=False, indent=2, default=str)
    return f"""
Ти — розумний AI-помічник для продавця на маркетплейсі. Магазин: {shop}.
Проаналізуй замовлення та історію чату з покупцем.

Дані замовлення (JSON):
{order_json}

Історія чату / повідомлення:
{chat_messages}

Поверни ЛИШЕ JSON-об'єкт без пояснень і без markdown з такими ключами:
- "customerName": ПІБ клієнта (рядок)
- "phone": телефон (рядок)
- "city": місто (рядок)
- "postOffice": відділення доставки (рядок)
- "products": список товарів з кількістю (масив рядків)
- "orderStatus": статус замовлення та наявність товарів (рядок)
- "chatAnalysis": короткий аналіз чату з покупцем (рядок)
- "recommendations": список рекомендацій для продавця, включно з коротким ввічливим варіантом відповіді покупцю (масив рядків)
"""


def parse_report_json(text):
    """Витягує JSON з відповіді Claude і доповнює відсутні поля порожніми значеннями."""
    start, end = text.find("{"), text.rfind("}")
    report = json.loads(text[start : end + 1])
    return {**EMPTY_REPORT, **report}


def process_order_and_chat_with_ai(shop, order_data, chat_messages):
    prompt = build_prompt(shop, order_data, chat_messages)
    response_text = ""

    try:
        response = client.messages.create(
            model="claude-haiku-5-5",
            max_tokens=1500,
            messages=[
                {"role": "user", "content": prompt}
            ]
        )

        # Безпечно збираємо текст із блоків відповіді (вирішує проблему ThinkingBlock)
        response_text = "".join([block.text for block in response.content if hasattr(block, "text")])
        return parse_report_json(response_text)

    except json.JSONDecodeError:
        print("Claude повернув відповідь, яку не вдалося розібрати як JSON")
        return {**EMPTY_REPORT, "chatAnalysis": response_text}
    except Exception as e:
        print(f"Помилка при зверненні до Claude API: {e}")
        return {**EMPTY_REPORT, "chatAnalysis": f"Помилка обробки даних через штучний інтелект: {str(e)}"}
