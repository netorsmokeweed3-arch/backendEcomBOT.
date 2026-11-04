import os
import requests
import logging

logger = logging.getLogger(__name__)

# Публічний / корпоративний API трекінгу Укрпошти
UP_API_URL = "https://www.ukrposhta.ua/e-commerce/0.0.1/barcodes"

def get_ukrposhta_tracking(ttn: str) -> dict:
    """
    Отримує статус посилки та історію подій для Укрпошти за штрих-кодом (ТТН).
    Використовує Master-токен Укрпошти зі змінних середовища Render.
    """
    bearer_token = os.getenv("UP_MASTER_API_KEY", "")
    
    headers = {
        "Accept": "application/json",
        "Authorization": f"Bearer {bearer_token}" if bearer_token else ""
    }
    
    try:
        response = requests.get(f"{UP_API_URL}/{ttn}", headers=headers, timeout=10)
        
        if response.status_code == 200:
            data = response.json()
            # Парсинг відповіді Укрпошти
            steps = data.get("steps", [])
            last_step = steps[-1] if steps else {}
            status_text = last_step.tzname if hasattr(last_step, "tzname") else "Обробляється Укрпоштою"
            
            timeline = [f"📮 Штрих-код Укрпошти: {ttn}", f"📍 Поточний статус: {status_text}"]
            for step in steps:
                if isinstance(step, dict):
                    timeline.append(f"📅 {step.get('date', '')}: {step.get('name', '')}")

            return {
                "success": True,
                "carrier": "Укрпошта",
                "trackingNumber": ttn,
                "status": status_text,
                "timelineEvents": timeline
            }
            
        return {"success": False, "error": "Відправлення Укрпошти не знайдено."}
        
    except Exception as e:
        logger.error(f"Помилка запиту до API Укрпошти: {str(e)}")
        return {"success": False, "error": f"Помилка з'єднання з Укрпоштою: {str(e)}"}