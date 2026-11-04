import os
import requests
import logging

logger = logging.getLogger(__name__)

NP_API_URL = "https://api.novaposhta.ua/v2.0/json/"

def get_nova_poshta_tracking(ttn: str) -> dict:
    """
    Отримує повний трекінг посилки, статус, вагу, суму післяплати та таймлайн за номером ТТН.
    Використовує єдиний корпоративний Master-токен із змінних середовища Render.
    """
    api_key = os.getenv("NP_MASTER_API_KEY", "")
    if not api_key:
        logger.warning("NP_MASTER_API_KEY не задано в середовищі бекенду!")
        return {"success": False, "error": "Поштові інтеграції налаштовуються адміністратором."}

    payload = {
        "apiKey": api_key,
        "modelName": "TrackingDocumentGeneral",
        "calledMethod": "getStatusDocuments",
        "methodProperties": {
            "Documents": [
                {
                    "DocumentNumber": ttn,
                    "Phone": ""
                }
            ]
        }
    }
    
    try:
        response = requests.post(NP_API_URL, json=payload, timeout=10)
        data = response.json()
        
        if data.get("success") and data.get("data"):
            info = data["data"][0]
            
            # Формуємо красивий таймлайн та деталі для продавця
            status_text = info.get("Status", "Статус оновлюється")
            warehouse_recipient = info.get("WarehouseRecipient", "")
            document_weight = info.get("DocumentWeight", "")
            back_delivery_sum = info.get("PatalogCurrency", "0")
            date_time = info.get("DateTimelabel", "")
            
            timeline = [
                f"📦 Зареєстровано в системі Нової Пошти",
                f"📍 Отримано/прямує до: {warehouse_recipient}" if warehouse_recipient else "📍 У дорозі",
                f"⚖️ Вага: {document_weight} кг" if document_weight else "",
                f"💰 Післяплата: {back_delivery_sum} грн" if float(back_delivery_sum or 0) > 0 else "✅ Оплачено",
                f"🔄 Поточний статус: {status_text} ({date_time})"
            ]
            
            return {
                "success": True,
                "carrier": "Нова Пошта",
                "trackingNumber": ttn,
                "status": status_text,
                "warehouse": warehouse_recipient,
                "weight": document_weight,
                "backDeliverySum": back_delivery_sum,
                "timelineEvents": [event for event in timeline if event]
            }
            
        return {"success": False, "error": "ТТН не знайдено або введено некоректно."}
        
    except Exception as e:
        logger.error(f"Помилка запиту до API Нової Пошти: {str(e)}")
        return {"success": False, "error": f"Помилка з'єднання з поштовим сервером: {str(e)}"}


    



















































    