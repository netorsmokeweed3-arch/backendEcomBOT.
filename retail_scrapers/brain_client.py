import logging
from typing import List, Dict, Any
from clients.base_client import BaseMarketplaceClient, OrderAPIError

logger = logging.getLogger(__name__)

class BrainClient(BaseMarketplaceClient):
    async def fetch_orders(self) -> List[Dict[str, Any]]:
        api_key = self.credentials.get("api_key", "") or self.credentials.get("token", "")
        if not api_key:
            raise OrderAPIError("Відсутній ключ доступу для Brain API.", 400)

        url = "https://api.brain.org.ua/orders" # Або корпоративний ендпоінт
        headers = {"X-Brain-API-Key": api_key}
        
        all_orders = []
        page = 1
        try:
            while True:
                params = {"page": page}
                if self.last_synced_at:
                    params["date_from"] = self.last_synced_at

                response = await self._make_request("GET", url, headers=headers, params=params)
                if response.status_code in (401, 403):
                    raise OrderAPIError("Помилка авторизації в Brain API.", response.status_code)
                if response.status_code != 200:
                    raise OrderAPIError(f"Brain API повернув код {response.status_code}", response.status_code)

                data = response.json()
                orders = data.get("orders", []) if isinstance(data, dict) else []
                if not orders:
                    break

                all_orders.extend(orders)
                page += 1
            return all_orders
        except OrderAPIError:
            raise
        except Exception as e:
            logger.error(f"Brain API error: {e}")
            raise OrderAPIError(f"Помилка Brain API: {e}", 502)