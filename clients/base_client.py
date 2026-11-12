"""Базовий абстрактний клас клієнта маркетплейсу (Enterprise Edition).
Забезпечує єдиний інтерфейс, таймаути, ретраї, уніфіковану обробку помилок
та вбудований захист від SSRF з асинхронним DNS-ресолвом.
"""

import abc
import logging
import httpx
import asyncio
import random
import ipaddress
import socket
from urllib.parse import urlparse
from typing import List, Dict, Any, Optional

logger = logging.getLogger(__name__)

class OrderAPIError(Exception):
    def __init__(self, message: str, status: int = 400):
        super().__init__(message)
        self.status = status

class BaseMarketplaceClient(abc.ABC):
    def __init__(self, shop_domain: str, credentials: Dict[str, Any], last_synced_at: Optional[str] = None):
        self.shop_domain = shop_domain.rstrip("/")
        self.credentials = credentials
        self.last_synced_at = last_synced_at
        self.timeout = 25.0
        self.max_retries = 3

    @abc.abstractmethod
    async def fetch_orders(self) -> List[Dict[str, Any]]:
        """Асинхронний метод для отримання сирих замовлень з маркетплейсу."""
        pass

    async def _validate_url_ssrf(self, target_url: str) -> None:
        """Перевіряє URL на предмет SSRF асинхронно."""
        try:
            parsed = urlparse(target_url)
            hostname = parsed.hostname
            if not hostname:
                raise OrderAPIError("Некоректний домен або URL для запиту.", 400)

            forbidden_keywords = ["localhost", "127.0.0.1", "0.0.0.0", "169.254.169.254", "metadata.google.internal"]
            if any(fk in hostname.lower() for fk in forbidden_keywords):
                logger.warning(f"Заблоковано потенційну SSRF-атаку на хост: {hostname}")
                raise OrderAPIError("Заборонено здійснювати запити на внутрішні або системні адреси.", 403)

            ip_str = await asyncio.to_thread(socket.gethostbyname, hostname)
            ip_obj = ipaddress.ip_address(ip_str)

            if (ip_obj.is_private or 
                ip_obj.is_loopback or 
                ip_obj.is_link_local or 
                ip_obj.is_reserved or 
                ip_obj.is_multicast):
                logger.warning(f"Заблоковано спробу SSRF на приватну/зарезервовану IP-адресу: {ip_str} ({hostname})")
                raise OrderAPIError("Доступ до приватних або локальних мереж заборонено.", 403)

        except socket.gaierror as err:
            raise OrderAPIError(f"Не вдалося резолвити домен: {hostname}", 400) from err
        except OrderAPIError:
            raise
        except Exception as e:
            logger.error(f"Помилка валідації SSRF для URL {target_url}: {e}")
            raise OrderAPIError("Помилка безпеки при перевірці домену.", 400)

    async def _make_request(self, method: str, url: str, **kwargs) -> httpx.Response:
        """Захищений асинхронний запит із перевіркою SSRF та ретраями."""
        await self._validate_url_ssrf(url)

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
                logger.warning(f"Збій запиту до {self.shop_domain} (спроба {attempt}/{self.max_retries}). Повтор...")
                await asyncio.sleep(sleep_time)
        
        raise OrderAPIError(f"Вичерпано спроби підключення до «{self.shop_domain}».", 504)