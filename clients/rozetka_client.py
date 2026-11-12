import logging
from typing import List, Dict, Any
from clients.base_client import BaseMarketplaceClient, OrderAPIError

logger = logging.getLogger(__name__)

class RozetkaClient(BaseMarketplaceClient):
    async def fetch_orders(self) -> List[Dict[str, Any]]:
        token = self.credentials.get("token", "")
        if not token:
            raise OrderAPIError("Відсутній API-токен для Rozetka.", 400)

        url = "https://api-seller.rozetka.com.ua/orders"
        headers = {"Authorization": f"Bearer {token}"}
        
        all_orders = []
        page = 1

        try:
            while True:
                params = {"page": page}
                if self.last_synced_at:
                    params["updated_at_min"] = self.last_synced_at
                
                response = await self._make_request("GET", url, headers=headers, params=params)
                
                if response.status_code in [401, 403]:
                    raise OrderAPIError("Токен доступу до Rozetka застарів або недійсний.", response.status_code)
                if response.status_code != 200:
                    raise OrderAPIError(f"Rozetka API повернув помилку: код {response.status_code}", response.status_code)
                    
                data = response.json()
                content = data.get("content", []) if isinstance(data, dict) else []
                
                if not content:
                    break
                    
                all_orders.extend(content)
                
                # Якщо розетка повертає менше ніж зазвичай за сторінку або порожньо — зупиняємось
                if len(content) < 50:
                    break
                    
                page += 1

            return all_orders
            
        except OrderAPIError:
            raise
        except Exception as e:
            logger.error(f"Rozetka API critical error for {self.shop_domain}: {str(e)}")
            raise OrderAPIError(f"Помилка зв'язку з Rozetka API: {str(e)}", 502)

