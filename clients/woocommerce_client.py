"""Модуль інтеграції з WooCommerce API."""

import logging
from typing import List, Dict, Any
from clients.base_client import BaseMarketplaceClient

logger = logging.getLogger(__name__)

class WooCommerceClient(BaseMarketplaceClient):
    async def fetch_orders(self) -> List[Dict[str, Any]]:
        ck = self.credentials.get("consumer_key", "")
        cs = self.credentials.get("consumer_secret", "")
        
        clean_url = self.shop_domain
        if not clean_url.startswith("http"):
            clean_url = f"https://{clean_url}"
            
        url = f"{clean_url}/wp-json/wc/v3/orders"
        
        try:
            response = await self._make_request("GET", url, auth=(ck, cs))
            if response.status_code == 200:
                data = response.json()
                return data if isinstance(data, list) else []
            logger.error(f"WooCommerce {self.shop_domain} відповів кодом {response.status_code}")
            return []
        except Exception as e:
            logger.error(f"Помилка запиту до WooCommerce {self.shop_domain}: {str(e)}")
            return []