"""Модуль інтеграції з WooCommerce REST API v3 (Enterprise Edition).
Підтримує інкрементальну синхронізацію та пагінацію.
"""

import logging
from typing import List, Dict, Any
from clients.base_client import BaseMarketplaceClient, OrderAPIError

logger = logging.getLogger(__name__)

class WooCommerceClient(BaseMarketplaceClient):
    async def fetch_orders(self) -> List[Dict[str, Any]]:
        ck = self.credentials.get("consumer_key", "")
        cs = self.credentials.get("consumer_secret", "")
        
        if not ck or not cs:
            raise OrderAPIError("Відсутні Consumer Key або Consumer Secret для WooCommerce.", 400)
        
        clean_url = self.shop_domain
        if not clean_url.startswith("http"):
            clean_url = f"https://{clean_url}"
            
        url = f"{clean_url}/wp-json/wc/v3/orders"
        
        all_orders = []
        page = 1
        per_page = 50

        try:
            while True:
                params = {"per_page": per_page, "page": page}
                if self.last_synced_at:
                    params["after"] = self.last_synced_at
                
                response = await self._make_request("GET", url, auth=(ck, cs), params=params)
                
                if response.status_code in [401, 403]:
                    raise OrderAPIError("Ключі доступу WooCommerce недійсні або застаріли.", response.status_code)
                if response.status_code != 200:
                    raise OrderAPIError(f"WooCommerce API повернув помилку: код {response.status_code}", response.status_code)
                    
                data = response.json()
                if not isinstance(data, list) or not data:
                    break
                    
                all_orders.extend(data)
                
                if len(data) < per_page:
                    break
                    
                page += 1

            return all_orders
                
        except OrderAPIError:
            raise
        except Exception as e:
            logger.error(f"Помилка запиту до WooCommerce {self.shop_domain}: {e}")
            raise OrderAPIError(f"Не вдалося отримати замовлення з WooCommerce: {e}", 502)