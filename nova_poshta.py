import requests
import logging

logger = logging.getLogger(__name__)

NP_API_URL = "https://api.novaposhta.ua/v2.0/json/"

def get_nova_poshta_status(ttn: str, api_key: str = None) -> dict:
    """
    Автоматичний трекінг Нової Пошти за номером ТТН із використанням Master-токена
    """
    payload = {
        "apiKey": api_key or "YOUR_MASTER_NP_API_KEY",
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
        if data.get("success"):
            info = data.get("data", [{}])[0]
            return {
                "success": True,
                "status": info.get("Status"),
                "warehouse": info.get("WarehouseRecipient"),
                "weight": info.get("DocumentWeight"),
                "back_delivery_sum": info.get("PatalogCurrency"),
                "timeline": info.get("DateTimelabel")
            }
        return {"success": False, "error": "Посилку не знайдено або невірний ТТН"}
    except Exception as e:
        logger.error(f"Помилка трекінгу НП: {str(e)}")
        return {"success": False, "error": str(e)}