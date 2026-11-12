import sys
import asyncio
import uuid

# Налаштування політики циклу подій для запобігання конфліктам на Windows
if sys.platform == "win32":
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

import pytest
import pytest_asyncio
from httpx import AsyncClient, ASGITransport
from sqlalchemy.ext.asyncio import create_async_engine, AsyncSession, async_sessionmaker
from server import app
from database import Base, get_db

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
    assert "token" in data

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
    token_a = reg_a.json()["token"]
    
    await async_client.post("/api/stores", json={
        "shop": "shop-a.prom.ua", 
        "platform": "prom", 
        "token": "secret-token-a"
    }, headers={"Authorization": f"Bearer {token_a}"})

    # Користувач Б реєструється і перевіряє свої магазини (має бути порожньо, бо ізоляція)
    email_b = f"b_{uuid.uuid4().hex[:6]}@test.io"
    reg_b = await async_client.post("/api/auth/register", json={"email": email_b, "password": "Password123!"})
    token_b = reg_b.json()["token"]
    
    stores_b = await async_client.get("/api/stores", headers={"Authorization": f"Bearer {token_b}"})
    assert stores_b.status_code == 200
    assert stores_b.json()["stores"] == []