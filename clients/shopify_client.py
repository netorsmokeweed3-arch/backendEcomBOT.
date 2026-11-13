"""Модуль інтеграції з Shopify API (Enterprise Edition) з підтримкою пагінації через Link Header."""

import logging
from typing import List, Dict, Any
from clients.base_client import BaseMarketplaceClient, OrderAPIError

logger = logging.getLogger(__name__)

class ShopifyClient(BaseMarketplaceClient):
    API_VERSION = "2024-10"

    async def fetch_orders(self) -> List[Dict[str, Any]]:
        token = self.credentials.get("token", "") or self.credentials.get("access_token", "")
        if not token:
            raise OrderAPIError("Відсутній Admin API Access Token для Shopify.", 400)

        domain = self.shop_domain
        if not domain.startswith("http"):
            domain = f"https://{domain}"

        url = f"{domain}/admin/api/{self.API_VERSION}/orders.json?status=any&limit=50"
        if self.last_synced_at:
            url += f"&updated_at_min={self.last_synced_at}"

        headers = {
            "X-Shopify-Access-Token": token,
            "Content-Type": "application/json"
        }

        all_orders = []

        try:
            current_url = url
            while current_url:
                response = await self._make_request("GET", current_url, headers=headers)
                
                if response.status_code in (401, 403):
                    raise OrderAPIError("Токен доступу до Shopify недійсний або застарів.", response.status_code)
                if response.status_code == 429:
                    raise OrderAPIError("Shopify API: перевищено ліміт запитів (Rate Limit).", 429)
                if response.status_code != 200:
                    raise OrderAPIError(f"Shopify API повернув помилку: код {response.status_code}", response.status_code)

                data = response.json()
                orders = data.get("orders", []) if isinstance(data, dict) else []
                if isinstance(orders, list) and orders:
                    all_orders.extend(orders)

                # Курсорна пагінація Shopify через заголовок Link
                link_header = response.headers.get("Link", "")
                next_url = None
                if link_header:
                    links = link_header.split(",")
                    for link in links:
                        if 'rel="next"' in link:
                            start_idx = link.find("<")
                            end_idx = link.find(">")
                            if start_idx != -1 and end_idx != -1:
                                next_url = link[start_idx + 1:end_idx]
                                break
                
                current_url = next_url

            return all_orders

        except OrderAPIError:
            raise
        except Exception as e:
            logger.error(f"Shopify API critical error for {self.shop_domain}: {e}")
            raise OrderAPIError(f"Помилка зв'язку з Shopify API: {e}", 502)