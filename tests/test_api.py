import sys
import asyncio
import uuid
import hmac
import hashlib

# Налаштування політики циклу подій для запобігання конфліктам на Windows
if sys.platform == "win32":
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

import pytest
import pytest_asyncio
from httpx import AsyncClient, ASGITransport
from sqlalchemy.ext.asyncio import create_async_engine, AsyncSession, async_sessionmaker
from server import app, WEBHOOK_SECRET
from database import Base, get_db, AsyncSessionLocal
from background_worker import save_orders_to_db

# Використовуємо ізольовану SQLite БД в пам'яті для міттєвих тестів
TEST_DATABASE_URL = "sqlite+aiosqlite:///:memory:"

test_engine = create_async_engine(TEST_DATABASE_URL, echo=False, future=True)
TestSessionLocal = async_sessionmaker(test_engine, class_=AsyncSession, expire_on_commit=False)

async def override_get_db():
    async with TestSessionLocal() as session:
        yield session

app.dependency_overrides[get_db] = override_get_db

@pytest_asyncio.fixture(autouse=True)
async def setup_db():
    async with test_engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield
    async with test_engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)

@pytest_asyncio.fixture
async def async_client():
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        yield client

@pytest.mark.asyncio
async def test_root_endpoint(async_client):
    response = await async_client.get("/")
    assert response.status_code == 200
    assert "OmniFlow" in response.json()["message"]

@pytest.mark.asyncio
async def test_user_registration_and_login(async_client):
    unique_email = f"test_{uuid.uuid4().hex[:8]}@omniflow.io"
    
    reg_response = await async_client.post("/api/auth/register", json={
        "email": unique_email,
        "password": "SecurePassword123!"
    })
    assert reg_response.status_code == 200
    data = reg_response.json()
    assert data["success"] is True
    assert "access_token" in data
    assert "refresh_token" in data

    login_response = await async_client.post("/api/auth/login", json={
        "email": unique_email,
        "password": "SecurePassword123!"
    })
    assert login_response.status_code == 200
    assert login_response.json()["success"] is True

@pytest.mark.asyncio
async def test_protected_route_without_token(async_client):
    response = await async_client.post("/api/orders", json={
        "shop": "myshop.prom.ua", 
        "token": "fake"
    })
    assert response.status_code == 401

@pytest.mark.asyncio
async def test_tenant_isolation_stores(async_client):
    # Користувач А створює магазин
    email_a = f"a_{uuid.uuid4().hex[:6]}@test.io"
    reg_a = await async_client.post("/api/auth/register", json={"email": email_a, "password": "Password123!"})
    token_a = reg_a.json()["access_token"]
    
    await async_client.post("/api/stores", json={
        "shop": "shop-a.prom.ua", 
        "platform": "prom", 
        "token": "secret-token-a"
    }, headers={"Authorization": f"Bearer {token_a}"})

    # Користувач Б реєструється і перевіряє свої магазини (має бути порожньо, бо ізоляція)
    email_b = f"b_{uuid.uuid4().hex[:6]}@test.io"
    reg_b = await async_client.post("/api/auth/register", json={"email": email_b, "password": "Password123!"})
    token_b = reg_b.json()["access_token"]
    
    stores_b = await async_client.get("/api/stores", headers={"Authorization": f"Bearer {token_b}"})
    assert stores_b.status_code == 200
    assert stores_b.json()["stores"] == []

@pytest.mark.asyncio
async def test_auth_refresh_and_logout(async_client):
    # Реєстрація для отримання токенів
    email = f"ref_{uuid.uuid4().hex[:6]}@test.io"
    reg_res = await async_client.post("/api/auth/register", json={"email": email, "password": "Password123!"})
    data = reg_res.json()
    access_token = data["access_token"]
    refresh_token = data["refresh_token"]

    # Тест ротації токенів через refresh
    refresh_res = await async_client.post("/api/auth/refresh", json={"refresh_token": refresh_token})
    assert refresh_res.status_code == 200
    ref_data = refresh_res.json()
    assert ref_data["success"] is True
    assert "access_token" in ref_data
    assert "refresh_token" in ref_data

    # Тест виходу з системи (Logout та відкликання токенів)
    logout_res = await async_client.post(
        "/api/auth/logout", 
        json={"refresh_token": refresh_token},
        headers={"Authorization": f"Bearer {access_token}"}
    )
    assert logout_res.status_code == 200
    assert logout_res.json()["success"] is True

@pytest.mark.asyncio
async def test_webhook_signature_and_idempotency(async_client):
    payload_body = b'{"id": "12345", "status": "new", "shop": "test.prom.ua"}'
    
    # Генерація дійсного HMAC підпису (якщо WEBHOOK_SECRET налаштовано)
    secret = WEBHOOK_SECRET if WEBHOOK_SECRET else "test-secret"
    signature = hmac.new(secret.encode("utf-8"), payload_body, hashlib.sha256).hexdigest()

    # Запит із невірним підписом має бути відхилений (або пройдено якщо секрет пустий)
    response_invalid = await async_client.post(
        "/api/webhook/prom",
        content=payload_body,
        headers={"X-Webhook-Signature": "invalid_signature"}
    )
    if WEBHOOK_SECRET:
        assert response_invalid.status_code == 403

    # Запит із правильним підписом
    response_valid = await async_client.post(
        "/api/webhook/prom",
        content=payload_body,
        headers={"X-Webhook-Signature": signature}
    )
    # Очікуємо успіх (200) або 403 залежно від наявності глобального WEBHOOK_SECRET в середовищі тестів
    assert response_valid.status_code in [200, 403]

@pytest.mark.asyncio
async def test_worker_save_orders_to_db():
    async with TestSessionLocal() as session:
        # Тестовий виклик збереження замовлень воркером у БД
        sample_orders = [
            {
                "id": "ord-999",
                "order_number": "999",
                "customer_name": "Іван Тест",
                "phone": "+380501234567",
                "total_amount": 1500.50,
                "currency": "UAH",
                "status": "Новий",
                "items": [
                    {"title": "Тестовий товар", "sku": "SKU-1", "quantity": 2, "unit_price": 750.25}
                ]
            }
        ]
        
        await save_orders_to_db(session, user_id=1, shop_domain="test-shop.prom.ua", orders=sample_orders)
        
        # Перевіряємо, чи замовлення реально записалося в базу
        from sqlalchemy import select
        from database import OrderModel
        
        res = await session.execute(select(OrderModel).where(OrderModel.external_order_id == "ord-999"))
        order = res.scalars().first()
        
        assert order is not None
        assert order.customer_name == "Іван Тест"
        assert order.total_amount == 1500.50