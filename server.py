import os
from flask import Flask, jsonify, request
import requests
from ai_service import process_order_and_chat_with_ai

app = Flask(__name__)

@app.route("/")
def home():
    return "EcomBot Multi-Store Backend is running successfully! 🚀"

# Універсальний маршрут для отримання замовлень із будь-якого магазину Shopify
@app.route("/api/orders", methods=["POST", "GET"])
def get_orders():
    # 1. Ловимо магазин і токен, які прилетіли з мобільного додатка
    # Додаток може передавати їх через query-параметри або в JSON-тілі запиту
    shop_domain = request.args.get("shop") or (request.json and request.json.get("shop"))
    access_token = request.args.get("token") or (request.json and request.json.get("token"))
    
    # Якщо з якихось причин додаток не передав їх, видаємо помилку для безпеки
    if not shop_domain or not access_token:
        return jsonify({
            "success": False,
            "message": "Не вказано назву магазину або API-токен!"
        }, 400)

    # 2. Формуємо динамічний URL для Shopify API конкретного користувача
    shopify_api_url = f"https://{shop_domain}.myshopify.com/admin/api/2024-01/orders.json?status=any"
    
    headers = {
        "X-Shopify-Access-Token": access_token,
        "Content-Type": "application/json",
    }

    try:
        # 3. Робимо запит до Shopify від імені цього магазину
        response = requests.get(shopify_api_url, headers=headers)

        if response.status_code == 200:
            data = response.json()
            orders = data.get("orders", [])
            return jsonify({"success": True, "orders": orders})
        else:
            print(f"Помилка Shopify API для магазину {shop_domain}: {response.status_code}, {response.text}")
            return jsonify({
                "success": False,
                "message": f"Помилка від Shopify API: {response.status_code}. Перевірте правильність токена чи назви магазину."
            }, 400)

    except Exception as e:
        print(f"Виняток при запиті до Shopify: {e}")
        return jsonify({"success": False, "error": str(e)}), 500

# Маршрут для обробки замовлень через Claude залишається незмінним
@app.route("/api/process-ai", methods=["POST"])
def process_ai():
    try:
        req_data = request.json
        order_data = req_data.get("order_data", "")
        chat_messages = req_data.get("chat_messages", "")

        ai_response = process_order_and_chat_with_ai(order_data, chat_messages)

        return jsonify({
            "success": True,
            "ai_report": ai_response
        })
    except Exception as e:
        print(f"Помилка ШІ маршруту: {e}")
        return jsonify({"success": False, "error": str(e)}), 500

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000)