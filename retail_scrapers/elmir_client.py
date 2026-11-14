import logging
from typing import List, Dict, Any
from clients.base_client import BaseMarketplaceClient, OrderAPIError

logger = logging.getLogger(__name__)

class ElmirClient(BaseMarketplaceClient):
    async def fetch_orders(self) -> List[Dict[str, Any]]:
        api_key = self.credentials.get("api_key", "")
        if not api_key:
            raise OrderAPIError("Відсутній ключ Elmir.", 400)

        url = "https://elmir.ua/api/orders"
        headers = {"X-Elmir-Key": api_key}
        
        response = await self._make_request("GET", url, headers=headers)
        if response.status_code != 200:
            raise OrderAPIError(f"Elmir API помилка: {response.status_code}", response.status_code)
            
        data = response.json()
        return data.get("orders", []) if isinstance(data, dict) else []