"""Базовий абстрактний клас клієнта маркетплейсу (Enterprise Edition).
Забезпечує єдиний інтерфейс, таймаути, ретраї та уніфіковану обробку помилок.
"""

import abc
import logging
import httpx
import asyncio
import random
from typing import List, Dict, Any

logger = logging.getLogger(__name__)

class OrderAPIError(Exception):
    def __init__(self, message: str, status: int = 400):
        super().__init__(message)
        self.status = status

class BaseMarketplaceClient(abc.ABC):
    def __init__(self, shop_domain: str, credentials: Dict[str, Any]):
        self.shop_domain = shop_domain.rstrip("/")
        self.credentials = credentials
        self.timeout = 25.0
        self.max_retries = 3

    @abc.abstractmethod
    async def fetch_orders(self) -> List[Dict[str, Any]]:
        """Асинхронний метод для отримання сирих замовлень з маркетплейсу."""
        pass

    async def _make_request(self, method: str, url: str, **kwargs) -> httpx.Response:
        """Захищений асинхронний запит із повторними спробами (Retry) та обробкою таймаутів."""
        retry_delay = 1.0
        for attempt in range(1, self.max_retries + 1):
            try:
                async with httpx.AsyncClient(timeout=self.timeout) as client:
                    response = await client.request(method, url, **kwargs)
                    return response
            except (httpx.TimeoutException, httpx.RequestError) as error:
                if attempt == self.max_retries:
                    logger.error(f"Мережевий збій для {self.shop_domain} після {self.max_retries} спроб: {error}")
                    raise OrderAPIError(f"Не вдалося підключитися до маркетплейсу «{self.shop_domain}».", 502) from error
                
                sleep_time = retry_delay * (2 ** (attempt - 1)) + random.uniform(0.1, 0.3)
                logger.warning(f"Збій запиту до {self.shop_domain} (спроба {attempt}/{self.max_retries}). Повтор через {sleep_time:.2f}с...")
                await asyncio.sleep(sleep_time)
        
        raise OrderAPIError(f"Вичерпано спроби підключення до «{self.shop_domain}».", 504)