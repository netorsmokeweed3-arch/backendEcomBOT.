import re
from flask import Flask, jsonify, request
import requests
from ai_service import process_order_and_chat_with_ai

app = Flask(__name__)

SHOPIFY_API_VERSION = "2024-01"
# Допустимі символи піддомену Shopify (наприклад: my-store)
SHOP_NAME_PATTERN = re.compile(r"^[a-z0-9][a-z0-9-]*$")


def normalize_shop_name(raw_shop):
    """Перетворює 'https://My-Store.myshopify.com/' або 'my-store' на 'my-store'."""
    if not raw_shop or not isinstance(raw_shop, str):
        return None
    shop = raw_shop.strip().lower()
    shop = re.sub(r"^https?://", "", shop)
    shop = shop.split("/")[0]
    if shop.endswith(".myshopify.com"):
        shop = shop[: -len(".myshopify.com")]
    if not SHOP_NAME_PATTERN.match(shop):
        return None
    return shop


def extract_shop_credentials():
    """Дістає shop і token з JSON-тіла POST, а якщо їх немає — з query-параметрів."""
    body = request.get_json(silent=True) or {}
    raw_shop = body.get("shop") or body.get("shop_domain") or request.args.get("shop")
    token = body.get("token") or body.get("access_token") or request.args.get("token")
    return raw_shop, token


@app.route("/")
def home():
    return "EcomBot Multi-Store Backend is running successfully! 🚀"


@app.route("/api/orders", methods=["POST", "GET"])
def get_orders():
    raw_shop, token = extract_shop_credentials()
    shop = normalize_shop_name(raw_shop)

    if not shop or not token:
        return jsonify({
            "success": False,
            "error": "Потрібно передати назву магазину (shop) і API-токен (token)."
        }), 400

    shopify_api_url = (
        f"https://{shop}.myshopify.com/admin/api/{SHOPIFY_API_VERSION}/orders.json?status=any"
    )
    headers = {
        "X-Shopify-Access-Token": token,
        "Content-Type": "application/json",
    }

    try:
        response = requests.get(shopify_api_url, headers=headers, timeout=25)

        if response.status_code == 200:
            orders = response.json().get("orders", [])
            return jsonify({"success": True, "shop": shop, "orders": orders})

        print(f"Помилка Shopify API для магазину {shop}: {response.status_code}, {response.text}")
        return jsonify({
            "success": False,
            "error": f"Помилка від Shopify API: {response.status_code}"
        }), 400

    except requests.RequestException as e:
        print(f"Виняток при запиті до Shopify ({shop}): {e}")
        return jsonify({"success": False, "error": "Не вдалося зв'язатися з Shopify."}), 502


@app.route("/api/process-ai", methods=["POST"])
def process_ai():
    try:
        req_data = request.get_json(silent=True) or {}
        shop = normalize_shop_name(req_data.get("shop") or req_data.get("shop_domain"))
        token = req_data.get("token") or req_data.get("access_token")
        order_data = req_data.get("order_data")
        chat_messages = req_data.get("chat_messages", "")

        if not shop or not token:
            return jsonify({
                "success": False,
                "error": "Потрібно передати назву магазину (shop) і API-токен (token)."
            }), 400
        if not order_data:
            return jsonify({"success": False, "error": "Не передано дані замовлення (order_data)."}), 400

        ai_report = process_order_and_chat_with_ai(
            shop=shop,
            order_data=order_data,
            chat_messages=chat_messages,
        )

        return jsonify({"success": True, "ai_report": ai_report})
    except Exception as e:
        print(f"Помилка ШІ маршруту: {e}")
        return jsonify({"success": False, "error": str(e)}), 500


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000)
