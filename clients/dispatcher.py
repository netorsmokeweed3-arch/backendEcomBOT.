"""Диспетчер маркетплейсів для паралельного збору замовлень та нормалізації (Enterprise Edition)."""

import asyncio
import logging
import sys
import os
from typing import List, Dict, Any

sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from order_normalizer import normalize_order_async
from clients.base_client import OrderAPIError

# Стандартні маркетплейси та CRM з папки clients/
from clients.prom_client import PromClient
from clients.rozetka_client import RozetkaClient
from clients.woocommerce_client import WooCommerceClient
from clients.shopify_client import ShopifyClient
from clients.olx_client import OlxClient
from clients.horoshop_client import HoroshopClient
from clients.izi_client import IziClient
from clients.keycrm_client import KeyCrmClient
from clients.kidstaff_client import KidstaffClient
from clients.remonline_client import RemonlineClient
from clients.shafa_client import ShafaClient
from clients.sqsp_client import SqspClient
from clients.weblium_client import WebliumClient
from clients.epicentr_client import EpicentrClient
from clients.crafta_client import CraftaClient
# Enterprise та ритейл-клієнти (частково в enterprise, частково в retail_scrapers)
from clients.enterprise.citrus_client import CitrusClient
from clients.enterprise.comfy_client import ComfyClient
from clients.enterprise.eldorado_client import EldoradoClient
from clients.enterprise.foxtrot_client import FoxtrotClient
from clients.enterprise.moyo_client import MoyoClient

# Ритейл-скрейпери з папки retail_scrapers/
from retail_scrapers.artline_client import ArtlineClient
from retail_scrapers.brain_client import BrainClient
from retail_scrapers.click_client import ClickClient
from retail_scrapers.elmir_client import ElmirClient
from retail_scrapers.exe_client import ExeClient
from retail_scrapers.kv_client import KvClient
from retail_scrapers.telemart_client import TelemartClient


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
            "olx": OlxClient,
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
                
                normalized = []
                for order in raw_orders:
                    if isinstance(order, dict):
                        norm_order = await normalize_order_async(order, shop_domain, platform)
                        normalized.append(norm_order)
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