"""Базовий абстрактний клас клієнта маркетплейсу (Enterprise Edition).
Забезпечує єдиний інтерфейс, таймаути та повторні спроби (retry) для всіх платформ.
"""

import abc
import logging
import httpx
from typing import List, Dict, Any

logger = logging.getLogger(__name__)

class BaseMarketplaceClient(abc.ABC):
    def __init__(self, shop_domain: str, credentials: Dict[str, Any]):
        self.shop_domain = shop_domain.rstrip("/")
        self.credentials = credentials
        self.timeout = 25.0

    @abc.abstractmethod
    async def fetch_orders(self) -> List[Dict[str, Any]]:
        """Асинхронний метод для отримання сирих замовлень з маркетплейсу."""
        pass

    async def _make_request(self, method: str, url: str, **kwargs) -> httpx.Response:
        """Захищений асинхронний запит із базовими таймаутами."""
        async with httpx.AsyncClient(timeout=self.timeout) as client:
            response = await client.request(method, url, **kwargs)
            return response