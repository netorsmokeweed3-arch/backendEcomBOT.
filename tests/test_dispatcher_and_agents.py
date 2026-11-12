import pytest
from unittest.mock import AsyncMock, patch
from httpx import Response, RequestError
from clients.dispatcher import MarketplaceDispatcher
from clients.prom_client import PromClient
from clients.rozetka_client import RozetkaClient
from clients.woocommerce_client import WooCommerceClient
from clients.base_client import OrderAPIError
from agents import MultiAgentSystem, _safe_parse_json


# ==========================================
# 1. ТЕСТИ ДЛЯ МАРКЕТПЛЕЙС-ДИСПЕТЧЕРА З MOCK'АННЯМ HTTPX
# ==========================================

@pytest.mark.asyncio
@patch("httpx.AsyncClient.request", new_callable=AsyncMock)
async def test_prom_client_fetch_orders_success(mock_request):
    """Тест успішного отримання замовлень з Prom через мок httpx."""
    mock_request.return_value = Response(
        status_code=200,
        json={"orders": [{"id": "PROM-123", "total_price": 500}]},
        request=None
    )
    
    client = PromClient("shop.prom.ua", {"token": "fake-token"})
    orders = await client.fetch_orders()
    
    assert len(orders) == 1
    assert orders[0]["id"] == "PROM-123"


@pytest.mark.asyncio
@patch("httpx.AsyncClient.request", new_callable=AsyncMock)
async def test_rozetka_client_unauthorized(mock_request):
    """Тест перехоплення помилки авторизації (401) для Rozetka."""
    mock_request.return_value = Response(
        status_code=401,
        json={"error": "Unauthorized"},
        request=None
    )
    
    client = RozetkaClient("rozetka.ua", {"token": "invalid-token"})
    
    with pytest.raises(OrderAPIError) as exc_info:
        await client.fetch_orders()
    
    assert exc_info.value.status == 401
    assert "застарів" in str(exc_info.value)


@pytest.mark.asyncio
@patch("httpx.AsyncClient.request", new_callable=AsyncMock)
async def test_woocommerce_client_success(mock_request):
    """Тест успішного отримання замовлень з WooCommerce з Basic Auth."""
    mock_request.return_value = Response(
        status_code=200,
        json=[{"id": 99, "total": "1200.00"}],
        request=None
    )
    
    client = WooCommerceClient("my-woo.com", {"consumer_key": "ck_test", "consumer_secret": "cs_test"})
    orders = await client.fetch_orders()
    
    assert len(orders) == 1
    assert orders[0]["id"] == 99


@pytest.mark.asyncio
@patch("clients.dispatcher.PromClient.fetch_orders", new_callable=AsyncMock)
async def test_marketplace_dispatcher_parallel_fetch(mock_prom_fetch):
    """Тест паралельного збору замовлень диспетчером з кількох магазинів."""
    mock_prom_fetch.return_value = [{"order_number": "ORD-1", "total_price": 100}]
    
    dispatcher = MarketplaceDispatcher(max_concurrent=3)
    stores = [
        {"platform": "prom", "shop": "store1.prom.ua", "credentials": {"token": "t1"}},
        {"platform": "prom", "shop": "store2.prom.ua", "credentials": {"token": "t2"}}
    ]
    
    all_orders = await dispatcher.fetch_all_user_stores(stores)
    
    assert len(all_orders) == 2
    assert all_orders[0]["order_number"] == "ORD-1"


# ==========================================
# 2. ТЕСТИ ДЛЯ ШІ-АГЕНТІВ (MultiAgentSystem)
# ==========================================

@pytest.mark.asyncio
@patch("agents.client.messages.create", new_callable=AsyncMock)
async def test_sales_agent_response(mock_anthropic):
    """Тест роботи Агента 1 (Продавець та Консультант)."""
    mock_anthropic.return_value.content = [type("obj", (object,), {"text": "Футболки є в наявності, синього кольору."})]
    
    reply = await MultiAgentSystem.run_sales_agent("Яка ціна футболки?", "Історія порожня")
    assert "Футболки є в наявності" in reply


@pytest.mark.asyncio
@patch("agents.client.messages.create", new_callable=AsyncMock)
async def test_support_agent_risk_evaluation(mock_anthropic):
    """Тест роботи Агента 3 (Підтримка та Повернення / Арбітр)."""
    mock_anthropic.return_value.content = [type("obj", (object,), {"text": '{"risk_level": "HIGH", "ai_suggested_solution": "Повернути кошти клієнту"}'})]
    
    result = await MultiAgentSystem.run_support_agent("Товар пошкоджений!", {"id": "123"})
    assert result["risk_level"] == "HIGH"
    assert "Повернути кошти" in result["ai_suggested_solution"]