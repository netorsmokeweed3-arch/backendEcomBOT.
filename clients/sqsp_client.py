import logging
from typing import List, Dict, Any
from clients.base_client import BaseMarketplaceClient, OrderAPIError

logger = logging.getLogger(__name__)

class SqspClient(BaseMarketplaceClient):
    async def fetch_orders(self) -> List[Dict[str, Any]]:
        api_key = self.credentials.get("api_key", "") or self.credentials.get("token", "")
        if not api_key:
            raise OrderAPIError("Відсутній API Key для Squarespace.", 400)

        domain = self.shop_domain if self.shop_domain.startswith("http") else f"https://{self.shop_domain}"
        url = f"{domain}/1.0/commerce/orders"
        headers = {"Authorization": f"Bearer {api_key}", "User-Agent": "SaaS-Integration/1.0"}

        all_orders = []
        cursor = None
        try:
            while True:
                params = {}
                if cursor:
                    params["cursor"] = cursor
                if self.last_synced_at:
                    params["modifiedAfter"] = self.last_synced_at

                response = await self._make_request("GET", url, headers=headers, params=params)
                if response.status_code in (401, 403):
                    raise OrderAPIError("Недійсні дані авторизації Squarespace.", response.status_code)
                if response.status_code != 200:
                    raise OrderAPIError(f"Squarespace API помилка: {response.status_code}", response.status_code)

                data = response.json()
                orders = data.get("result", []) if isinstance(data, dict) else []
                if not orders:
                    break
                all_orders.extend(orders)

                pagination = data.get("pagination", {})
                if pagination.get("hasNextPage", False):
                    cursor = pagination.get("nextPageCursor")
                else:
                    break

            return all_orders
        except OrderAPIError:
            raise
        except Exception as e:
            logger.error(f"Squarespace error: {e}")
            raise OrderAPIError(f"Помилка Squarespace API: {e}", 502)