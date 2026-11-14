import logging
from typing import List, Dict, Any
from clients.base_client import BaseMarketplaceClient, OrderAPIError

logger = logging.getLogger(__name__)

class RemonlineClient(BaseMarketplaceClient):
    async def fetch_orders(self) -> List[Dict[str, Any]]:
        api_key = self.credentials.get("api_key", "") or self.credentials.get("token", "")
        if not api_key:
            raise OrderAPIError("Відсутній API-ключ для RemOnline.", 400)

        url = "https://api.remonline.app/order/"
        params = {"api_key": api_key}
        
        if self.last_synced_at:
            # RemOnline приймає timestamp або дату в певних форматах
            params["updated_since"] = self.last_synced_at

        try:
            response = await self._make_request("GET", url, params=params)
            if response.status_code in (401, 403):
                raise OrderAPIError("Недійсний API-ключ RemOnline.", response.status_code)
            if response.status_code != 200:
                raise OrderAPIError(f"RemOnline API помилка: код {response.status_code}", response.status_code)

            data = response.json()
            return data.get("data", []) if isinstance(data, dict) else []
        except OrderAPIError:
            raise
        except Exception as e:
            logger.error(f"RemOnline error: {e}")
            raise OrderAPIError(f"Помилка зв'язку з RemOnline: {e}", 502)