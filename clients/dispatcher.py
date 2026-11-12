import asyncio
import logging
import sys
import os
from typing import List, Dict, Any

sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

try:
    from order_normalizer import normalize_order, OrderAPIError
except ImportError:
    def normalize_order(raw: dict, shop: str) -> dict:
        return raw
    class OrderAPIError(Exception):
        def __init__(self, message: str, status: int = 400):
            super().__init__(message)
            self.status = status

from clients.prom_client import PromClient
from clients.rozetka_client import RozetkaClient
from clients.woocommerce_client import WooCommerceClient
from clients.shopify_client import ShopifyClient

logger = logging.getLogger(__name__)

class MarketplaceDispatcher:
    def __init__(self, max_concurrent: int = 5):
        self.semaphore = asyncio.Semaphore(max_concurrent)

    def _get_client(self, platform: str, shop_domain: str, credentials: Dict[str, Any], last_synced_at: str = None):
        """Фабрика створення клієнта з передачею інкрементальної мітки часу."""
        p = platform.lower()
        if "prom" in p:
            return PromClient(shop_domain, credentials, last_synced_at=last_synced_at)
        elif "rozetka" in p:
            return RozetkaClient(shop_domain, credentials, last_synced_at=last_synced_at)
        elif "woo" in p or "wordpress" in p:
            return WooCommerceClient(shop_domain, credentials, last_synced_at=last_synced_at)
        elif "shopify" in p:
            return ShopifyClient(shop_domain, credentials, last_synced_at=last_synced_at)
        
        logger.warning(f"Невідома платформа для клієнта: {platform}")
        raise OrderAPIError(f"Платформа «{platform}» не підтримується.", 400)

    async def fetch_store_orders(self, store_config: Dict[str, Any]) -> List[Dict[str, Any]]:
        platform = store_config.get("platform", "prom")
        shop_domain = store_config.get("shop") or store_config.get("shop_domain", "")
        credentials = store_config.get("credentials", {})
        last_synced_at = store_config.get("last_synced_at")

        client = self._get_client(platform, shop_domain, credentials, last_synced_at=last_synced_at)

        async with self.semaphore:
            try:
                logger.info(f"Починаємо опитування магазину: {shop_domain} ({platform}) [Last synced: {last_synced_at}]")
                raw_orders = await client.fetch_orders()
                
                normalized = [
                    normalize_order(order, shop_domain)
                    for order in raw_orders if isinstance(order, dict)
                ]
                return normalized
            except OrderAPIError:
                raise
            except Exception as e:
                logger.error(f"Помилка збору замовлень з магазину {shop_domain}: {str(e)}")
                return []

    async def fetch_all_user_stores(self, stores_list: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        if not isinstance(stores_list, list) or not stores_list:
            return []

        tasks = [self.fetch_store_orders(store) for store in stores_list]
        results = await asyncio.gather(*tasks, return_exceptions=True)

        all_orders = []
        for res in results:
            if isinstance(res, list):
                all_orders.extend(res)
            elif isinstance(res, Exception):
                logger.error(f"Задача диспетчера завершилася винятком: {res}")

        return all_orders