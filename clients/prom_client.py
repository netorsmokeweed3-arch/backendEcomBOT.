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
        
        try:
            response = await self._make_request("GET", url, headers=headers)
            
            if response.status_code in [401, 403]:
                logger.warning(f"Помилка авторизації Prom для магазину {self.shop_domain}")
                raise OrderAPIError("Токен доступу до Prom застарів або недійсний.", response.status_code)
                
            if response.status_code != 200:
                raise OrderAPIError(f"Prom API повернув помилку: код {response.status_code}", response.status_code)
                
            try:
                data = response.json()
            except ValueError as err:
                raise OrderAPIError("Prom API повернув невалідний JSON.", 502) from err

            return data.get("orders", []) if isinstance(data, dict) else []
            
        except OrderAPIError:
            raise
        except Exception as e:
            logger.error(f"Prom API critical error for {self.shop_domain}: {str(e)}")
            raise OrderAPIError(f"Помилка зв'язку з Prom API: {str(e)}", 502)