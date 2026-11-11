"""Юніт-тести для перевірки нормалізатора замовлень OmniFlow.
Використовується бібліотека pytest. Запуск: pytest test_normalizer.py
"""

import pytest
from order_normalizer import normalize_order, normalize_shop, money, to_number


def test_normalize_shop_urls():
    """Тест очищення та нормалізації доменів магазинів."""
    assert normalize_shop("https://my-store.com/path") == "my-store.com"
    assert normalize_shop("http://shop.ua/") == "shop.ua"
    assert normalize_shop("STORE.COM") == "store.com"
    assert normalize_shop(None) is None


def test_money_formatting():
    """Тест безпечного форматування грошових сум і валют."""
    res_uah = money(1500.5, "UAH")
    assert res_uah["amount"] == 1500.5
    assert res_uah["currency"] == "UAH"
    assert "1500.50" in res_uah["formatted"]

    res_usd = money("99.99", "USD")
    assert res_usd["amount"] == 99.99
    assert res_usd["currency"] == "USD"
    assert "$" in res_usd["formatted"]


def test_to_number_conversion():
    """Тест конвертації значень у числові формати."""
    assert to_number("123.45") == 123.45
    assert to_number("1 234,56") == 1234.56
    assert to_number(None) is None
    assert to_number(False) is None


def test_normalize_order_basic():
    """Тест нормалізації сирого об'єкта замовлення з нетиповими ключами."""
    raw_order = {
        "order_id": "ORD-999",
        "total_price": "2500",
        "currency": "uah",
        "customer": {
            "first_name": "Іван",
            "last_name": "Петренко",
            "email": "ivan@example.com"
        },
        "line_items": [
            {
                "name": "Тестова футболка",
                "quantity": 2,
                "price": 1250,
                "sku": "TSHIRT-01"
            }
        ]
    }

    normalized = normalize_order(raw_order, "mystore.com")

    assert normalized["shop"] == "mystore.com"
    assert normalized["order_number"] == "ORD-999"
    assert normalized["customer_name"] == "Іван Петренко"
    assert normalized["email"] == "ivan@example.com"
    assert normalized["total_amount"]["amount"] == 2500.0
    assert len(normalized["items"]) == 1
    assert normalized["items"][0]["title"] == "Тестова футболка"
    assert normalized["items"][0]["quantity"] == 2