import logging
from typing import List, Dict, Any
from clients.base_client import BaseMarketplaceClient, OrderAPIError

logger = logging.getLogger(__name__)

class PromClient(BaseMarketplaceClient):
    async def fetch_orders(self) -> List[Dict[str, Any]]:
        token = self.credentials.get("token", "")
        if not token:
            raise OrderAPIError("Відсутній API-токен для Prom.", 400)

        url = "https://my.prom.ua/api/v1/orders/list"
        headers = {"Authorization": f"Bearer {token}"}
        
        all_orders = []
        page = 1
        limit = 50

        try:
            while True:
                params = {"page": page, "limit": limit}
                if self.last_synced_at:
                    params["updated_at_min"] = self.last_synced_at
                
                response = await self._make_request("GET", url, headers=headers, params=params)
                
                if response.status_code in [401, 403]:
                    raise OrderAPIError("Токен доступу до Prom застарів або недійсний.", response.status_code)
                if response.status_code != 200:
                    raise OrderAPIError(f"Prom API повернув помилку: код {response.status_code}", response.status_code)
                    
                data = response.json()
                orders = data.get("orders", []) if isinstance(data, dict) else []
                
                if not orders:
                    break
                    
                all_orders.extend(orders)
                
                # Якщо замовлень повернулось менше ліміту — це була остання сторінка
                if len(orders) < limit:
                    break
                    
                page += 1

            return all_orders
            
        except OrderAPIError:
            raise
        except Exception as e:
            logger.error(f"Prom API critical error for {self.shop_domain}: {str(e)}")
            raise OrderAPIError(f"Помилка зв'язку з Prom API: {str(e)}", 502)
