from flask import Flask, jsonify, request

from ai_service import analyze_order
from order_normalizer import OrderAPIError, fetch_orders, normalize_order, normalize_shop

app = Flask(__name__)


def read_body() -> dict:
    """JSON-тіло запиту; для GET беремо query-параметри."""
    body = request.get_json(silent=True)
    return body if isinstance(body, dict) else request.args.to_dict()


def read_credentials(body: dict):
    """Назва магазину і токен з запиту. Нічого не захардкоджено."""
    shop = normalize_shop(body.get("shop") or body.get("shop_domain"))
    token = body.get("token") or body.get("access_token")
    # Діагностика: токен не виводимо, лише довжину та префікс для перевірки формату
    print(f"[DEBUG] Вхідні дані: shop={shop!r}, token_len={len(token) if isinstance(token, str) else 'N/A'}, "
          f"token_prefix={token[:6] if isinstance(token, str) else 'N/A'}", flush=True)
    if not shop or not token:
        raise OrderAPIError("Потрібно передати назву магазину (shop) і API-токен (token).", 400)
    return shop, token


def error_response(error: Exception):
    status = error.status if isinstance(error, OrderAPIError) else 500
    return jsonify({"success": False, "error": str(error)}), status


@app.route("/")
def home():
    return "EcomBot Multi-Store Backend is running successfully! 🚀"


@app.route("/api/orders", methods=["POST", "GET"])
def get_orders():
    try:
        body = read_body()
        shop, token = read_credentials(body)
        raw_orders = fetch_orders(shop, token, body.get("orders_url"))
        orders = [normalize_order(raw, shop) for raw in raw_orders if isinstance(raw, dict)]
        return jsonify({"success": True, "shop": shop, "orders": orders})
    except Exception as error:
        print(f"Помилка /api/orders: {error}")
        return error_response(error)


@app.route("/api/process-ai", methods=["POST"])
def process_ai():
    try:
        body = read_body()
        shop, _token = read_credentials(body)
        order_data = body.get("order_data")
        if not isinstance(order_data, dict):
            raise OrderAPIError("Не передано дані замовлення (order_data).", 400)

        # Сире замовлення нормалізуємо тим самим обробником, що і /api/orders
        order = order_data if "delivery_address" in order_data and "total_amount" in order_data \
            else normalize_order(order_data, shop)
        ai_report = analyze_order(order, body.get("chat_messages", ""))
        return jsonify({"success": True, "ai_report": ai_report})
    except Exception as error:
        print(f"Помилка ШІ маршруту: {error}")
        return error_response(error)


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000)
