"""Модуль інтеграції з OLX Partner API (Enterprise Edition)."""

import logging
from typing import List, Dict, Any
from clients.base_client import BaseMarketplaceClient, OrderAPIError

logger = logging.getLogger(__name__)

class OlxClient(BaseMarketplaceClient):
    async def fetch_orders(self) -> List[Dict[str, Any]]:
        token = self.credentials.get("token", "") or self.credentials.get("access_token", "")
        if not token:
            raise OrderAPIError("Відсутній OAuth-токен або API-ключ для OLX.", 400)

        # Офіційний базовий ендпоінт OLX Partner API для замовлень
        url = "https://www.olx.ua/api/partner/orders"
        headers = {
            "Authorization": f"Bearer {token}",
            "Version": "2.0",
            "Content-Type": "application/json"
        }
        
        all_orders = []
        page = 1
        limit = 50

        try:
            while True:
                params = {"page": page, "limit": limit}
                if self.last_synced_at:
                    params["filter[date_from]"] = self.last_synced_at

                response = await self._make_request("GET", url, headers=headers, params=params)
                
                if response.status_code in (401, 403):
                    raise OrderAPIError("Токен доступу до OLX недійсний або прострочений.", response.status_code)
                if response.status_code != 200:
                    raise OrderAPIError(f"OLX API повернув помилку: код {response.status_code}", response.status_code)
                    
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
            logger.error(f"OLX API critical error for {self.shop_domain}: {e}")
            raise OrderAPIError(f"Помилка зв'язку з OLX API: {e}", 502)