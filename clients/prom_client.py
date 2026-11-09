import logging
from typing import List, Dict, Any
from clients.base_client import BaseMarketplaceClient

logger = logging.getLogger(__name__)

class PromClient(BaseMarketplaceClient):
    async def fetch_orders(self) -> List[Dict[str, Any]]:
        token = self.credentials.get("token", "")
        url = "https://my.prom.ua/api/v1/orders/list"
        headers = {"Authorization": f"Bearer {token}"}
        
        try:
            response = await self._make_request("GET", url, headers=headers)
            if response.status_code == 200:
                data = response.json()
                return data.get("orders", []) if isinstance(data, dict) else []
            return []
        except Exception as e:
            logger.error(f"Prom API error for {self.shop_domain}: {str(e)}")
            return []