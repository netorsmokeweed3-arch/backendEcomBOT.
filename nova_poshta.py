"""Модуль інтеграції з API Нової Пошти (Enterprise Edition).
Повністю асинхронний трекінг посилок через httpx.AsyncClient з обробкою винятків та захистом від збоїв.
"""

import os
import logging
from typing import Any, Dict
import httpx

logger = logging.getLogger(__name__)

NP_API_URL = "https://api.novaposhta.ua/v2.0/json/"


async def get_nova_poshta_tracking(ttn: str) -> Dict[str, Any]:
    """Асинхронно отримує статус, вагу, післяплату та історію посилки за номером ТТН.
    Захищений від таймаутів, невалідного JSON та відсутності ключів у середовищі.
    """
    if not isinstance(ttn, str) or not ttn.strip():
        return {"success": False, "error": "Некоректний номер ТТН."}

    clean_ttn = ttn.strip()
    api_key = os.getenv("NP_MASTER_API_KEY", "")
    
    if not api_key:
        logger.warning("NP_MASTER_API_KEY не задано в середовищі бекенду!")
        return {"success": False, "error": "Поштова інтеграція налаштовується адміністратором."}

    payload = {
        "apiKey": api_key,
        "modelName": "TrackingDocumentGeneral",
        "calledMethod": "getStatusDocuments",
        "methodProperties": {
            "Documents": [
                {
                    "DocumentNumber": clean_ttn,
                    "Phone": ""
                }
            ]
        }
    }
    
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            response = await client.post(NP_API_URL, json=payload)
            
        if response.status_code != 200:
            logger.error(f"Помилка API Нової Пошти: статус {response.status_code}")
            return {"success": False, "error": f"Поштова служба тимчасово недоступна (код {response.status_code})."}

        try:
            data = response.json()
        except ValueError:
            logger.error("Нова Пошта повернула невалідний JSON")
            return {"success": False, "error": "Помилка обробки відповіді від Нової Пошти."}
        
        if data.get("success") and isinstance(data.get("data"), list) and data["data"]:
            info = data["data"][0]
            if not isinstance(info, dict):
                return {"success": False, "error": "Отримано невірну структуру даних від Нової Пошти."}
            
            status_text = str(info.get("Status", "Статус оновлюється"))
            warehouse_recipient = str(info.get("WarehouseRecipient", ""))
            document_weight = str(info.get("DocumentWeight", ""))
            back_delivery_sum = str(info.get("AfterpaymentOnGoodsSum", "0"))
            date_time = str(info.get("DateTimelabel", ""))
            
            try:
                parsed_back_delivery = float(back_delivery_sum)
            except (ValueError, TypeError):
                parsed_back_delivery = 0.0

            timeline = [
                "📦 Зареєстровано в системі Нової Пошти",
                f"📍 Отримано/прямує до: {warehouse_recipient}" if warehouse_recipient else "📍 У дорозі",
                f"⚖️ Вага: {document_weight} кг" if document_weight else "",
                f"💰 Післяплата: {back_delivery_sum} грн" if parsed_back_delivery > 0 else "✅ Оплачено",
                f"🔄 Поточний статус: {status_text} ({date_time})"
            ]
            
            return {
                "success": True,
                "carrier": "Нова Пошта",
                "trackingNumber": clean_ttn,
                "status": status_text,
                "warehouse": warehouse_recipient,
                "weight": document_weight,
                "backDeliverySum": back_delivery_sum,
                "timelineEvents": [event for event in timeline if event]
            }
            
        errors = data.get("errors", [])
        error_msg = errors[0] if errors else "ТТН не знайдено або введено некоректно."
        return {"success": False, "error": error_msg}
        
    except httpx.TimeoutException:
        logger.error(f"Таймаут з'єднання при запиті ТТН {clean_ttn} до Нової Пошти")
        return {"success": False, "error": "Час очікування відповіді від Нової Пошти вичерпано."}
    except Exception as e:
        logger.error(f"Критична помилка запиту до API Нової Пошти: {e}", exc_info=True)
        return {"success": False, "error": "Внутрішня помилка при перевірці посилки Нової Пошти."}











































    