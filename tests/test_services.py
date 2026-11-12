import pytest
from unittest.mock import AsyncMock, patch
from clients.dispatcher import MarketplaceDispatcher
from ai_service import analyze_order
from agents import _safe_parse_json


@pytest.mark.asyncio
async def test_dispatcher_handles_unknown_platform():
    dispatcher = MarketplaceDispatcher(max_concurrent=2)
    store_config = {
        "platform": "unknown_marketplace",
        "shop": "test.ua",
        "credentials": {"token": "123"}
    }
    # Перевіряємо, що диспетчер коректно обробляє невідому платформу або повертає порожній список/помилку
    result = await dispatcher.fetch_all_user_stores([store_config])
    assert isinstance(result, list)


def test_safe_parse_json_extracts_from_markdown():
    raw_ai_output = '''Ось аналіз замовлення:
    ```json
    {"ai_summary": "Клієнт просить швидку доставку", "cancelled": false}
    ```
    Гарного дня!'''
    parsed = _safe_parse_json(raw_ai_output)
    assert parsed["ai_summary"] == "Клієнт просить швидку доставку"
    assert parsed["cancelled"] is False


@pytest.mark.asyncio
@patch("ai_service.MultiAgentSystem.run_analytics_agent", new_callable=AsyncMock)
async def test_analyze_order_pipeline(mock_analytics):
    mock_analytics.return_value = {
        "ai_summary": "Тестове замовлення підтверджено",
        "customer_name": "Андрій"
    }
    sample_order = {
        "id": "1001",
        "shop": "test.prom.ua",
        "total_amount": {"amount": 1500, "currency": "UAH", "formatted": "1500.00 грн"},
        "items": [{"title": "Кросівки", "quantity": 1, "line_total": "1500.00 грн"}]
    }
    report = await analyze_order(sample_order, "Коли відправка?")
    assert report["ai_summary"] == "Тестове замовлення підтверджено"
    assert report["customer_name"] == "Андрій"