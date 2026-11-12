import logging
from typing import List, Dict, Any
from clients.base_client import BaseMarketplaceClient, OrderAPIError

logger = logging.getLogger(__name__)

class ShopifyClient(BaseMarketplaceClient):
    """Клієнт для інтеграції з Shopify Admin API (Orders)."""
    
    API_VERSION = "2024-10"

    async def fetch_orders(self) -> List[Dict[str, Any]]:
        """Отримує список замовлень із Shopify Store через Admin API."""
        token = self.credentials.get("token", "") or self.credentials.get("access_token", "")
        if not token:
            raise OrderAPIError("Відсутній Admin API Access Token для Shopify.", 400)

        domain = self.shop_domain
        if not domain.startswith("http"):
            domain = f"https://{domain}"

        url = f"{domain}/admin/api/{self.API_VERSION}/orders.json?status=any&limit=50"
        headers = {
            "X-Shopify-Access-Token": token,
            "Content-Type": "application/json"
        }

        try:
            response = await self._make_request("GET", url, headers=headers)
            
            if response.status_code in (401, 403):
                raise OrderAPIError("Токен доступу до Shopify недійсний або застарів.", response.status_code)
            if response.status_code == 429:
                raise OrderAPIError("Shopify API: перевищено ліміт запитів (Rate Limit).", 429)
            if response.status_code != 200:
                raise OrderAPIError(f"Shopify API повернув помилку: код {response.status_code}", response.status_code)

            try:
                data = response.json()
            except ValueError as err:
                raise OrderAPIError("Shopify API повернув невалідний JSON.", 502) from err

            orders = data.get("orders", []) if isinstance(data, dict) else []
            return orders if isinstance(orders, list) else []

        except OrderAPIError:
            raise
        except Exception as e:
            logger.error(f"Shopify API critical error for {self.shop_domain}: {e}")
            raise OrderAPIError(f"Помилка зв'язку з Shopify API: {e}", 502)