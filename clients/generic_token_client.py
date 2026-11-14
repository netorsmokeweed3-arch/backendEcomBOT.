"""Універсальний клієнт для платформ із токен-авторизацією (DRY-патерн, Enterprise Edition)."""

import logging
from typing import List, Dict, Any, Optional
from clients.base_client import BaseMarketplaceClient, OrderAPIError

logger = logging.getLogger(__name__)

class GenericTokenClient(BaseMarketplaceClient):
    """Базовий клас для платформ, що використовують стандартний Token/Bearer запит."""
    
    # Дочірні класи можуть перевизначати цей URL за потреби
    base_endpoint: Optional[str] = None
    token_credential_keys: tuple = ("token", "access_token", "api_key", "bearer_token")

    def _extract_token(self) -> str:
        """Безпечно витягує токен з різних можливих ключів у credentials."""
        for key in self.token_credential_keys:
            token = self.credentials.get(key)
            if token and isinstance(token, str):
                token_val = token.strip()
                if token_val:
                    return token_val
        return ""

    async def fetch_orders(self) -> List[Dict[str, Any]]:
        token = self._extract_token()
        if not token:
            raise OrderAPIError(f"Відсутній або невалідний токен доступу для магазину «{self.shop_domain}».", 400)

        # Якщо base_endpoint не задано спеціально, формуємо стандартний шлях на основі домену
        url = self.base_endpoint or f"https://{self.shop_domain}/api/v1/orders"
        
        headers = {
            "Authorization": f"Bearer {token}",
            "Accept": "application/json",
            "Content-Type": "application/json"
        }

        try:
            response = await self._make_request("GET", url, headers=headers)
            
            if response.status_code in (401, 403):
                logger.warning(f"Помилка авторизації (статус {response.status_code}) для магазину {self.shop_domain}")
                raise OrderAPIError("Недійсний або прострочений токен доступу до маркетплейсу.", response.status_code)
            
            if response.status_code != 200:
                raise OrderAPIError(f"API маркетплейсу повернув помилку: статус {response.status_code}", response.status_code)

            try:
                data = response.json()
            except Exception as json_err:
                logger.error(f"Невалідний JSON від {self.shop_domain}: {json_err}")
                raise OrderAPIError("Маркетплейс повернув некоректну відповідь (не JSON).", 502) from json_err

            # Універсальне витягування масиву замовлень із різних форматів відповіді
            if isinstance(data, list):
                return data
            
            if isinstance(data, dict):
                for key in ("orders", "data", "results", "items", "content", "response", "list"):
                    val = data.get(key)
                    if isinstance(val, list):
                        return val
                # Якщо бекенд повернув єдиний об'єкт замовлення без обгортки масиву
                if any(k in data for k in ("id", "order_id", "number", "orderNumber")):
                    return [data]

            return []

            return []

        except OrderAPIError:
            raise
        except Exception as e:
            logger.error(f"Критична помилка в GenericTokenClient для {self.shop_domain}: {e}")
            raise OrderAPIError(f"Помилка зв'язку з API маркетплейсу: {e}", 502)