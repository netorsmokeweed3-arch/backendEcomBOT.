import logging
from typing import List, Dict, Any
from clients.base_client import BaseMarketplaceClient, OrderAPIError

logger = logging.getLogger(__name__)

class ComfyClient(BaseMarketplaceClient):
    async def fetch_orders(self) -> List[Dict[str, Any]]:
        token = self.credentials.get("token", "")
        if not token:
            raise OrderAPIError("Відсутній токен для Comfy API.", 400)

        url = "https://partner.comfy.ua/api/orders"
        headers = {"Authorization": f"Bearer {token}"}
        
        all_orders = []
        page = 1
        try:
            while True:
                params = {"page": page}
                if self.last_synced_at:
                    params["from_date"] = self.last_synced_at

                response = await self._make_request("GET", url, headers=headers, params=params)
                if response.status_code in (401, 403):
                    raise OrderAPIError("Токен Comfy недійсний.", response.status_code)
                if response.status_code != 200:
                    raise OrderAPIError(f"Comfy API код: {response.status_code}", response.status_code)

                data = response.json()
                orders = data.get("result", []) if isinstance(data, dict) else []
                if not orders:
                    break
                all_orders.extend(orders)
                page += 1
            return all_orders
        except OrderAPIError:
            raise
        except Exception as e:
            logger.error(f"Comfy error: {e}")
            raise OrderAPIError(f"Помилка Comfy API: {e}", 502)