"""Диспетчер маркетплейсів для паралельного збору замовлень та нормалізації (Enterprise Edition)."""

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
from clients.olx_client import OlxClient  # <-- Додано імпорт OLX

# Імпорт решти публічних, enterprise та ритейл-клієнтів
from retail_scrapers.artline_client import ArtlineClient
from retail_scrapers.brain_client import BrainClient
from clients.enterprise.citrus_client import CitrusClient
from retail_scrapers.click_client import ClickClient
from clients.enterprise.comfy_client import ComfyClient
from clients.crafta_client import CraftaClient
from clients.enterprise.eldorado_client import EldoradoClient
from retail_scrapers.elmir_client import ElmirClient
from clients.epicentr_client import EpicentrClient
from retail_scrapers.exe_client import ExeClient
from clients.enterprise.foxtrot_client import FoxtrotClient
from clients.horoshop_client import HoroshopClient
from clients.izi_client import IziClient
from clients.keycrm_client import KeyCrmClient
from clients.kidstaff_client import KidstaffClient
from retail_scrapers.kv_client import KvClient
from clients.enterprise.moyo_client import MoyoClient
from clients.remonline_client import RemonlineClient
from clients.shafa_client import ShafaClient
from clients.sqsp_client import SqspClient
from retail_scrapers.telemart_client import TelemartClient
from clients.weblium_client import WebliumClient

logger = logging.getLogger(__name__)

class MarketplaceDispatcher:
    def __init__(self, max_concurrent: int = 5):
        self.semaphore = asyncio.Semaphore(max_concurrent)

    def _get_client(self, platform: str, shop_domain: str, credentials: Dict[str, Any], last_synced_at: str = None):
        p = platform.lower()
        
        registry = {
            "prom": PromClient,
            "rozetka": RozetkaClient,
            "woo": WooCommerceClient,
            "woocommerce": WooCommerceClient,
            "shopify": ShopifyClient,
            "olx": OlxClient,  # <-- Зареєстровано OLX у словнику
            "artline": ArtlineClient,
            "brain": BrainClient,
            "citrus": CitrusClient,
            "click": ClickClient,
            "comfy": ComfyClient,
            "crafta": CraftaClient,
            "eldorado": EldoradoClient,
            "elmir": ElmirClient,
            "epicentr": EpicentrClient,
            "exe": ExeClient,
            "foxtrot": FoxtrotClient,
            "horoshop": HoroshopClient,
            "izi": IziClient,
            "keycrm": KeyCrmClient,
            "kidstaff": KidstaffClient,
            "kv": KvClient,
            "moyo": MoyoClient,
            "remonline": RemonlineClient,
            "shafa": ShafaClient,
            "sqsp": SqspClient,
            "squarespace": SqspClient,
            "telemart": TelemartClient,
            "weblium": WebliumClient,
        }

        for key, client_cls in registry.items():
            if key in p:
                return client_cls(shop_domain, credentials, last_synced_at=last_synced_at)

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
                logger.error(f"Помилка збору замовлень з магазину {shop_domain}: {e}")
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