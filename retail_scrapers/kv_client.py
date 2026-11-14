import logging
from typing import List, Dict, Any
from clients.base_client import BaseMarketplaceClient, OrderAPIError

logger = logging.getLogger(__name__)

class KvClient(BaseMarketplaceClient):
    async def fetch_orders(self) -> List[Dict[str, Any]]:
        token = self.credentials.get("token", "")
        if not token:
            raise OrderAPIError("Відсутній токен KV.", 400)

        domain = self.shop_domain if self.shop_domain.startswith("http") else f"https://{self.shop_domain}"
        url = f"{domain}/api/orders"
        headers = {"Authorization": f"Bearer {token}"}
        
        response = await self._make_request("GET", url, headers=headers)
        if response.status_code != 200:
            raise OrderAPIError(f"KV API помилка: {response.status_code}", response.status_code)
            
        data = response.json()
        return data.get("orders", []) if isinstance(data, dict) else []