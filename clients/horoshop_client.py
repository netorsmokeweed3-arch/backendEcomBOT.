import logging
from typing import List, Dict, Any
from clients.base_client import BaseMarketplaceClient, OrderAPIError

logger = logging.getLogger(__name__)

class HoroshopClient(BaseMarketplaceClient):
    async def fetch_orders(self) -> List[Dict[str, Any]]:
        login = self.credentials.get("login", "")
        password = self.credentials.get("password", "") or self.credentials.get("api_key", "")
        
        if not login or not password:
            raise OrderAPIError("Відсутні облікові дані (login/password) для Хорошоп.", 400)

        domain = self.shop_domain if self.shop_domain.startswith("http") else f"https://{self.shop_domain}"
        url = f"{domain}/api/get/orders/"
        
        payload = {"login": login, "password": password}
        if self.last_synced_at:
            payload["updated_since"] = self.last_synced_at

        try:
            response = await self._make_request("POST", url, json=payload)
            if response.status_code in (401, 403):
                raise OrderAPIError("Невірний логін або пароль Хорошоп.", response.status_code)
            if response.status_code != 200:
                raise OrderAPIError(f"Horoshop API помилка: код {response.status_code}", response.status_code)

            data = response.json()
            return data.get("orders", []) if isinstance(data, dict) else []
        except OrderAPIError:
            raise
        except Exception as e:
            logger.error(f"Horoshop error for {self.shop_domain}: {e}")
            raise OrderAPIError(f"Помилка зв'язку з Хорошоп: {e}", 502)