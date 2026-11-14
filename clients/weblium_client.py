import logging
from typing import List, Dict, Any
from clients.base_client import BaseMarketplaceClient, OrderAPIError

logger = logging.getLogger(__name__)

class WebliumClient(BaseMarketplaceClient):
    async def fetch_orders(self) -> List[Dict[str, Any]]:
        token = self.credentials.get("token", "") or self.credentials.get("api_key", "")
        if not token:
            raise OrderAPIError("Відсутній токен Weblium.", 400)

        url = "https://api.weblium.v1/orders"
        headers = {"Authorization": f"Bearer {token}"}
        
        response = await self._make_request("GET", url, headers=headers)
        if response.status_code != 200:
            raise OrderAPIError(f"Weblium API помилка: {response.status_code}", response.status_code)
            
        data = response.json()
        return data.get("orders", []) if isinstance(data, dict) else []