import logging
from typing import List, Dict, Any
from clients.base_client import BaseMarketplaceClient, OrderAPIError

logger = logging.getLogger(__name__)

class KeyCrmClient(BaseMarketplaceClient):
    async def fetch_orders(self) -> List[Dict[str, Any]]:
        token = self.credentials.get("token", "") or self.credentials.get("api_key", "")
        if not token:
            raise OrderAPIError("Відсутній API-ключ для KeyCRM.", 400)

        url = "https://openapi.keycrm.app/v1/orders"
        headers = {"Authorization": f"Bearer {token}", "Accept": "application/json"}
        
        all_orders = []
        page = 1
        try:
            while True:
                params = {"page": page, "limit": 50}
                if self.last_synced_at:
                    params["filter[updated_at]"] = self.last_synced_at

                response = await self._make_request("GET", url, headers=headers, params=params)
                if response.status_code in (401, 403):
                    raise OrderAPIError("Недійсний API-ключ KeyCRM.", response.status_code)
                if response.status_code != 200:
                    raise OrderAPIError(f"KeyCRM API помилка: код {response.status_code}", response.status_code)

                data = response.json()
                orders = data.get("data", []) if isinstance(data, dict) else []
                if not orders:
                    break
                all_orders.extend(orders)
                
                # Перевірка пагінації KeyCRM
                meta = data.get("meta", {})
                current_page = meta.get("current_page", page)
                last_page = meta.get("last_page", page)
                if current_page >= last_page:
                    break
                page += 1

            return all_orders
        except OrderAPIError:
            raise
        except Exception as e:
            logger.error(f"KeyCRM error: {e}")
            raise OrderAPIError(f"Помилка зв'язку з KeyCRM: {e}", 502)