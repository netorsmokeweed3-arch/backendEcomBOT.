import json
import logging
import random
import asyncio
from datetime import datetime, timedelta
from flask import Flask, jsonify, request
from werkzeug.exceptions import BadRequest, UnsupportedMediaType

from ai_service import analyze_order
from order_normalizer import OrderAPIError, fetch_orders, normalize_order, normalize_shop

# Професійне структуроване логування
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S"
)
logger = logging.getLogger(__name__)

app = Flask(__name__)

BACKEND_VERSION = "2.3.3-ai-store-chat"

# Жорсткий ліміт розміру тіла запиту (2 МБ)
app.config['MAX_CONTENT_LENGTH'] = 2 * 1024 * 1024 


def read_body() -> dict:
    try:
        if request.is_json:
            body = request.get_json(silent=True)
            if isinstance(body, dict):
                return body
        if request.form:
            return request.form.to_dict()
        return request.args.to_dict()
    except BadRequest:
        raise OrderAPIError("Некоректний формат JSON у тілі запиту.", 400)


def read_credentials(body: dict):
    raw_shop = body.get("shop") or body.get("shop_domain")
    raw_token = body.get("token") or body.get("access_token")

    shop = normalize_shop(raw_shop.strip() if isinstance(raw_shop, str) else raw_shop)
    token = raw_token.strip() if isinstance(raw_token, str) else raw_token

    if not shop or not token:
        raise OrderAPIError("Потрібно обов'язково передати назву магазину (shop) і API-токен (token).", 400)
    
    return shop, token


def error_response(error: Exception):
    status = error.status if isinstance(error, OrderAPIError) else 500
    error_message = str(error) if status < 500 else "Внутрішня помилка сервера. Спробуйте пізніше."
    if status >= 500:
        logger.error("Критична помилка сервера: %s", error, exc_info=True)
    return jsonify({"success": False, "error": error_message}), status


@app.errorhandler(400)
def handle_bad_request(_error):
    return jsonify({"success": False, "error": "Невірний формат запиту (Bad Request)."}), 400

@app.errorhandler(413)
def handle_too_large(_error):
    return jsonify({"success": False, "error": "Завеликий обсяг даних у запиті."}), 413

@app.errorhandler(UnsupportedMediaType)
def handle_unsupported_media(_error):
    return jsonify({"success": False, "error": "Непідтримуваний формат даних (очікується application/json)."}), 415


@app.route("/")
def home():
    return f"EcomBot AI Store Backend is running successfully! (v: {BACKEND_VERSION}) 🚀"


@app.route("/api/orders", methods=["POST", "GET"])
def get_orders():
    try:
        body = read_body()
        shop, token = read_credentials(body)
        
        # --- START MOCK STORE & 100 ORDERS GENERATOR ---
        if "test" in token.lower() or "demo" in token.lower() or "ai" in token.lower() or shop == "ai-store":
            logger.info("Активовано інтелектуальний тест-каталог (100 замовлень + чати ШІ) для магазину: %s", shop)
            
            mock_orders = [
                {
                    "id": "AI-ORD-001",
                    "order_number": "2001",
                    "created_at": (datetime.now() - timedelta(hours=1)).isoformat() + "Z",
                    "customer_name": "Андрій Шевченко",
                    "phone": "+380501112233",
                    "email": "andriy@example.com",
                    "delivery_method": "Нова Пошта (відділення №1)",
                    "delivery_address": {
                        "street": "вул. Городоцька, 10",
                        "city": "Львів",
                        "region": "Львівська область",
                        "postal_code": "79016",
                        "country": "Україна",
                        "full": "м. Львів, вул. Городоцька, 10, відд. №1"
                    },
                    "total_amount": {"amount": 350.00, "currency": "UAH", "formatted": "350.00 грн"},
                    "currency": "UAH",
                    "status": {"text": "new", "cancelled": False},
                    "items": [
                        {
                            "id": "t-green",
                            "title": "Футболка бавовняна (Зелена - Акція)",
                            "sku": "TSHIRT-GRN-ACT",
                            "quantity": 1,
                            "unit_price": {"amount": 350.00, "currency": "UAH", "formatted": "350.00 грн"},
                            "line_total": {"amount": 350.00, "currency": "UAH", "formatted": "350.00 грн"}
                        }
                    ],
                    "chatHistory": "Покупець: Добрий день, хочу замовити чорну футболку. \nШІ: Вітаю! На жаль, чорні футболки зараз розпродані, але маємо чудову зелену футболку з акційною ціною всього 350 грн замість 500 грн! Бажаєте оформити? \nПокупець: Добре, давайте зелену по акції, оформлюйте на Львів, відд. №1."
                }
            ]

            first_names = ["Іван", "Софія", "Олександр", "Марія", "Дмитро", "Вікторія", "Павло", "Анастасія", "Артем", "Катерина"]
            last_names = ["Петренко", "Мельник", "Ковальчук", "Шевченко", "Бойко", "Кравченко", "Ткаченко", "Олійник"]
            cities = ["Київ", "Львів", "Одеса", "Харків", "Дніпро", "Запоріжжя", "Вінниця", "Полтава"]
            products_pool = [
                ("Футболка бавовняна (Зелена - Акція)", 350),
                ("Футболка бавовняна (Синя)", 500),
                ("Футболка бавовняна (Біла)", 500),
                ("Футболка бавовняна (Жовта)", 500),
            ]

            for i in range(4, 101):
                p_name, p_price = random.choice(products_pool)
                qty = random.choice([1, 1, 1, 2])
                total = p_price * qty
                c_name = f"{random.choice(first_names)} {random.choice(last_names)}"
                city = random.choice(cities)
                
                mock_orders.append({
                    "id": f"AI-ORD-{i:03d}",
                    "order_number": str(2000 + i),
                    "created_at": (datetime.now() - timedelta(hours=random.randint(6, 120))).isoformat() + "Z",
                    "customer_name": c_name,
                    "phone": f"+380{random.randint(50, 99)}{random.randint(1000000, 9999999)}",
                    "email": f"user{i}@example.com",
                    "delivery_method": "Нова Пошта",
                    "delivery_address": {
                        "street": f"вул. Центральна, {random.randint(1, 100)}",
                        "city": city,
                        "region": f"{city} область",
                        "postal_code": f"{random.randint(10000, 99999)}",
                        "country": "Україна",
                        "full": f"м. {city}, вул. Центральна"
                    },
                    "total_amount": {"amount": float(total), "currency": "UAH", "formatted": f"{total}.00 грн"},
                    "currency": "UAH",
                    "status": {"text": random.choice(["new", "processing", "completed"]), "cancelled": False},
                    "items": [
                        {
                            "id": f"item-{i}",
                            "title": p_name,
                            "sku": f"SKU-{i}",
                            "quantity": qty,
                            "unit_price": {"amount": float(p_price), "currency": "UAH", "formatted": f"{p_price}.00 грн"},
                            "line_total": {"amount": float(total), "currency": "UAH", "formatted": f"{total}.00 грн"}
                        }
                    ],
                    "chatHistory": f"Автоматичне замовлення клієнта {c_name} через каталог магазину. Оплата при отриманні."
                })

            orders = [normalize_order(raw, shop) for raw in mock_orders if isinstance(raw, dict)]
            logger.info("Успішно згенеровано та завантажено замовлень: %d для магазину: %s", len(orders), shop)
            return jsonify({"success": True, "shop": shop, "orders": orders})
        # --- END MOCK STORE ---

        orders_url = body.get("orders_url")
        if orders_url is not None and not isinstance(orders_url, str):
            orders_url = str(orders_url)

        raw_orders = fetch_orders(shop, token, orders_url)
        if not isinstance(raw_orders, list):
            raw_orders = []

        orders = [normalize_order(raw, shop) for raw in raw_orders if isinstance(raw, dict)]
        logger.info("Успішно завантажено замовлень: %d для магазину: %s", len(orders), shop)
        
        return jsonify({"success": True, "shop": shop, "orders": orders})
        
    except Exception as error:
        return error_response(error)


@app.route("/api/process-ai", methods=["POST"])
def process_ai():
    try:
        body = read_body()
        shop, _token = read_credentials(body)
        order_data = body.get("order_data")
        
        if not isinstance(order_data, dict):
            raise OrderAPIError("Не передано або пошкоджено дані замовлення (order_data має бути об'єктом).", 400)

        if "delivery_address" in order_data and "total_amount" in order_data:
            order = order_data
        else:
            order = normalize_order(order_data, shop)
            
        chat_messages = body.get("chat_messages", "")
        if not isinstance(chat_messages, (str, list)):
            chat_messages = str(chat_messages)

        ai_report = asyncio.run(analyze_order(order, chat_messages))
        logger.info("ШІ-аналіз успішно завершено для замовлення ID: %s", order.get('id', 'unknown'))
        
        return jsonify({"success": True, "ai_report": ai_report})
        
    except Exception as error:
        return error_response(error)


# --- СИНХРОННИЙ МАРШРУТ ДЛЯ ІНТЕРАКТИВНОГО ЧАТУ З ШІ (З ASYNCIO.RUN) ---
@app.route("/api/chat", methods=["POST"])
def chat_with_ai():
    try:
        body = read_body()
        shop, _token = read_credentials(body)
        user_message = body.get("message", "")
        chat_history = body.get("history", "")
        
        if not user_message:
            raise OrderAPIError("Повідомлення не може бути порожнім.", 400)

        catalog_context = (
            "Каталог магазину одягу:\n"
            "1. Футболка бавовняна (Чорна) - НЕМАЄ В НАЯВНОСТІ.\n"
            "2. Футболка бавовняна (Зелена - Акція) - В наявності, акційна ціна 350 грн (замість 500 грн).\n"
            "3. Футболка бавовняна (Синя) - В наявності, ціна 500 грн.\n"
            "4. Футболка бавовняна (Біла) - В наявності, ціна 500 грн (при замовленні від 2 штук — безкоштовна доставка).\n"
            "Твоя задача — бути ввічливим продавцем-консультантом, пропонувати альтернативи, якщо товару немає, та допомагати покупцю."
        )

        full_prompt = f"{catalog_context}\n\nІсторія діалогу:\n{chat_history}\n\nПокупець: {user_message}\nШІ-консультант:"
        
        dummy_order = {"id": "chat-session", "customer_name": "Покупець у чаті", "items": []}
        
        # Безпечний синхронний виклик асинхронної функції через asyncio.run
        ai_report = asyncio.run(analyze_order(dummy_order, full_prompt))
        
        ai_reply = ai_report.get("ai_summary") or ai_report.get("recommendation") or ai_report.get("summary") or "Вітаю! Чим можу допомогти з вибором футболок?"

        logger.info("Успішно згенеровано відповідь ШІ у чаті для магазину: %s", shop)
        return jsonify({"success": True, "reply": ai_reply})
        
    except Exception as error:
        return error_response(error)


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000)