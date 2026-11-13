"""Модуль інтеграції з API Укрпошти (Enterprise Edition).
Повністю асинхронний трекінг відправлень через httpx.AsyncClient з глибокою валідацією.
"""

import os
import logging
from typing import Any, Dict
import httpx

logger = logging.getLogger(__name__)

UP_API_URL = "https://www.ukrposhta.ua/e-commerce/0.0.1/barcodes"


async def get_ukrposhta_tracking(ttn: str) -> Dict[str, Any]:
    """Асинхронно отримує статус відправлення та історію подій для Укрпошти за штрих-кодом (ТТН).
    Захищений від збоїв структури JSON та мережевих таймаутів.
    """
    if not isinstance(ttn, str) or not ttn.strip():
        return {"success": False, "error": "Некоректний номер штрих-коду Укрпошти."}

    clean_ttn = ttn.strip()
    bearer_token = os.getenv("UP_MASTER_API_KEY", "")
    
    headers = {
        "Accept": "application/json",
    }
    if bearer_token:
        headers["Authorization"] = f"Bearer {bearer_token}"
    
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            response = await client.get(f"{UP_API_URL}/{clean_ttn}", headers=headers)
        
        if response.status_code != 200:
            logger.error(f"Помилка API Укрпошти для ТТН {clean_ttn}: статус {response.status_code}")
            return {"success": False, "error": "Відправлення Укрпошти не знайдено або сервер недоступний."}

        try:
            data = response.json()
        except ValueError:
            logger.error("Укрпошта повернула невалідний JSON")
            return {"success": False, "error": "Помилка обробки відповіді від Укрпошти."}

        # Безпечний витяг кроків (steps) залежно від формату відповіді API
        steps = []
        if isinstance(data, dict):
            steps = data.get("steps", [])
        elif isinstance(data, list):
            steps = data

        if not isinstance(steps, list):
            steps = []

        last_step = steps[-1] if steps and isinstance(steps[-1], dict) else {}
        status_text = str(last_step.get("name") or last_step.get("status") or "Обробляється Укрпоштою")
        
        timeline = [f"📮 Штрих-код Укрпошти: {clean_ttn}", f"📍 Поточний статус: {status_text}"]
        
        for step in steps:
            if isinstance(step, dict):
                step_date = str(step.get("date", ""))
                step_name = str(step.get("name", ""))
                if step_name:
                    timeline.append(f"📅 {step_date}: {step_name}" if step_date else f"📅 {step_name}")

        return {
            "success": True,
            "carrier": "Укрпошта",
            "trackingNumber": clean_ttn,
            "status": status_text,
            "timelineEvents": timeline
        }
        
    except httpx.TimeoutException:
        logger.error(f"Таймаут з'єднання при запиті ТТН {clean_ttn} до Укрпошти")
        return {"success": False, "error": "Час очікування відповіді від Укрпошти вичерпано."}
    except Exception as e:
        logger.error(f"Критична помилка запиту до API Укрпошти: {e}", exc_info=True)
        return {"success": False, "error": "Внутрішня помилка при перевірці посилки Укрпошти."}