"""Тести для перевірки реального модуля order_normalizer.py."""

import pytest
from order_normalizer import normalize_shop, normalize_order, to_number, money


def test_normalize_shop_strips_protocol():
    assert normalize_shop("https://My-Shop.prom.ua/path") == "my-shop.prom.ua"
    assert normalize_shop("") is None
    assert normalize_shop(None) is None


def test_normalize_order_shopify_structure():
    raw = {
        "id": 450789469, 
        "order_number": 1001, 
        "currency": "USD", 
        "total_price": "749.95",
        "customer": {
            "first_name": "Andrii", 
            "last_name": "Bolshakov", 
            "email": "a@example.com"
        },
        "shipping_address": {
            "address1": "Verbutskogo 15/11", 
            "city": "Portland", 
            "zip": "97201", 
            "country": "United States"
        },
        "line_items": [
            {
                "id": 1, 
                "title": "Desk Lamp", 
                "sku": "LAMP-01", 
                "quantity": 1, 
                "price": "749.95"
            }
        ],
    }
    result = normalize_order(raw, "test.myshopify.com")
    assert result["customer_name"] == "Andrii Bolshakov"
    assert result["total_amount"]["amount"] == 749.95
    assert result["items"][0]["sku"] == "LAMP-01"
    assert result["delivery_address"]["city"] == "Portland"


def test_to_number_converts_correctly():
    assert to_number(" 1,500.50 ") == 1500.50
    assert to_number(100) == 100.0
    assert to_number("invalid") is None


def test_money_formatting():
    res = money("500", "UAH")
    assert res["amount"] == 500.0
    assert res["currency"] == "UAH"
    assert "500.00" in res["formatted"]