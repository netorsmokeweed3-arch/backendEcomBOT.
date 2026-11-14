import logging
from typing import List, Dict, Any
from clients.base_client import BaseMarketplaceClient, OrderAPIError

logger = logging.getLogger(__name__)

class ArtlineClient(BaseMarketplaceClient):
    async def fetch_orders(self) -> List[Dict[str, Any]]:
        token = self.credentials.get("token", "") or self.credentials.get("api_key", "")
        if not token:
            raise OrderAPIError("Відсутній API-токен для Artline.", 400)

        domain = self.shop_domain if self.shop_domain.startswith("http") else f"https://{self.shop_domain}"
        url = f"{domain}/api/v1/orders"
        headers = {"Authorization": f"Bearer {token}"}
        
        all_orders = []
        page = 1
        limit = 50

        try:
            while True:
                params = {"page": page, "limit": limit}
                if self.last_synced_at:
                    params["updated_since"] = self.last_synced_at

                response = await self._make_request("GET", url, headers=headers, params=params)
                if response.status_code in (401, 403):
                    raise OrderAPIError("Недійсний токен доступу Artline.", response.status_code)
                if response.status_code != 200:
                    raise OrderAPIError(f"Artline API помилка: код {response.status_code}", response.status_code)

                data = response.json()
                orders = data.get("data", []) if isinstance(data, dict) else (data if isinstance(data, list) else [])
                if not orders:
                    break

                all_orders.extend(orders)
                if len(orders) < limit:
                    break
                page += 1

            return all_orders
        except OrderAPIError:
            raise
        except Exception as e:
            logger.error(f"Artline error for {self.shop_domain}: {e}")
            raise OrderAPIError(f"Помилка зв'язку з Artline API: {e}", 502)