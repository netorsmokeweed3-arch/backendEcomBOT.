import logging
from typing import List, Dict, Any
from clients.base_client import BaseMarketplaceClient, OrderAPIError

logger = logging.getLogger(__name__)

class EpicentrClient(BaseMarketplaceClient):
    async def fetch_orders(self) -> List[Dict[str, Any]]:
        token = self.credentials.get("token", "")
        if not token:
            raise OrderAPIError("Відсутній токен Епіцентр (Marketplace API).", 400)

        url = "https://api.epicentrk.ua/merchants/v1/orders"
        headers = {"Authorization": f"Bearer {token}"}
        
        all_orders = []
        page = 1
        try:
            while True:
                params = {"page": page}
                if self.last_synced_at:
                    params["updated_at_min"] = self.last_synced_at

                response = await self._make_request("GET", url, headers=headers, params=params)
                if response.status_code in (401, 403):
                    raise OrderAPIError("Недійсний токен Епіцентр API.", response.status_code)
                if response.status_code != 200:
                    raise OrderAPIError(f"Epicentr API код: {response.status_code}", response.status_code)

                data = response.json()
                orders = data.get("data", []) if isinstance(data, dict) else []
                if not orders:
                    break
                all_orders.extend(orders)
                page += 1
            return all_orders
        except OrderAPIError:
            raise
        except Exception as e:
            logger.error(f"Epicentr error: {e}")
            raise OrderAPIError(f"Помилка Epicentr API: {e}", 502)