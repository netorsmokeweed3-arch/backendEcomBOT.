import logging
from typing import List, Dict, Any
from clients.base_client import BaseMarketplaceClient, OrderAPIError

logger = logging.getLogger(__name__)

class CitrusClient(BaseMarketplaceClient):
    async def fetch_orders(self) -> List[Dict[str, Any]]:
        token = self.credentials.get("token", "")
        if not token:
            raise OrderAPIError("Відсутній токен для Citrus.", 400)

        domain = self.shop_domain if self.shop_domain.startswith("http") else f"https://{self.shop_domain}"
        url = f"{domain}/api/orders"
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
                    raise OrderAPIError("Недійсний токен Citrus.", response.status_code)
                if response.status_code != 200:
                    raise OrderAPIError(f"Citrus API помилка: код {response.status_code}", response.status_code)

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
            logger.error(f"Citrus API error: {e}")
            raise OrderAPIError(f"Помилка зв'язку з Citrus: {e}", 502)