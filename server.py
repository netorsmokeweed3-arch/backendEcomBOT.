import json
import logging
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

BACKEND_VERSION = "2.2.6-bulletproof"

# Жорсткий ліміт розміру тіла запиту (2 МБ) для захисту від перевантаження пам'яті
app.config['MAX_CONTENT_LENGTH'] = 2 * 1024 * 1024 


def read_body() -> dict:
    """Безпечне читання вхідних даних із повним захистом від зіпсованого JSON."""
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
    """Витягування, очищення та сувора валідація облікових даних."""
    raw_shop = body.get("shop") or body.get("shop_domain")
    raw_token = body.get("token") or body.get("access_token")

    # Санітизація (очищення від випадкових пробілів на кінцях)
    shop = normalize_shop(raw_shop.strip() if isinstance(raw_shop, str) else raw_shop)
    token = raw_token.strip() if isinstance(raw_token, str) else raw_token

    token_str = str(token) if token else ""
    logger.info(
        "Вхідні дані: shop=%r, token_len=%d, token_prefix=%s",
        shop,
        len(token_str) if token else 0,
        token_str[:6] if token else "N/A"
    )

    if not shop or not token:
        raise OrderAPIError("Потрібно обов'язково передати назву магазину (shop) і API-токен (token).", 400)
    
    return shop, token


def error_response(error: Exception):
    """Глобальний форматувач помилок для клієнта."""
    status = error.status if isinstance(error, OrderAPIError) else 500
    error_message = str(error) if status < 500 else "Внутрішня помилка сервера. Спробуйте пізніше."
    
    if status >= 500:
        logger.error("Критична помилка сервера: %s", error, exc_info=True)
        
    return jsonify({"success": False, "error": error_message}), status


# Глобальні перехоплювачі системних помилок Flask для уникнення HTML-відповідей
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
    return f"EcomBot Multi-Store Backend is running successfully! (v: {BACKEND_VERSION}) 🚀"


@app.route("/api/orders", methods=["POST", "GET"])
def get_orders():
    try:
        body = read_body()
        shop, token = read_credentials(body)
        
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
        
        try:
            logger.info("DEBUG RAW ORDER_DATA RECEIVED: %s", json.dumps(order_data, ensure_ascii=False))
        except Exception:
            logger.info("DEBUG RAW ORDER_DATA RECEIVED: %r", order_data)

        if not isinstance(order_data, dict):
            raise OrderAPIError("Не передано або пошкоджено дані замовлення (order_data має бути об'єктом).", 400)

        # Перевірка структури замовлення перед нормалізацією
        if "delivery_address" in order_data and "total_amount" in order_data:
            order = order_data
        else:
            order = normalize_order(order_data, shop)
            
        chat_messages = body.get("chat_messages", "")
        if not isinstance(chat_messages, (str, list)):
            chat_messages = str(chat_messages)

        ai_report = analyze_order(order, chat_messages)
        logger.info("ШІ-аналіз успішно завершено для замовлення ID: %s", order.get('id', 'unknown'))
        
        return jsonify({"success": True, "ai_report": ai_report})
        
    except Exception as error:
        return error_response(error)


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000)