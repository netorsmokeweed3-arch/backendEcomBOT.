"""Модуль бази даних для SaaS-платформи (Enterprise Edition - Hardened & Atomic).
Асинхронний SQLAlchemy рушій з пулом з'єднань, захистом від гонки потоків,
WAL-режимом для SQLite та підтримкою атомарних транзакцій (Race Condition Prevention).
"""

import os
import logging
from typing import AsyncGenerator
from sqlalchemy.ext.asyncio import create_async_engine, AsyncSession, async_sessionmaker
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column
from sqlalchemy import String, Text, event, select
from sqlalchemy.pool import AsyncAdaptedQueuePool

logger = logging.getLogger(__name__)

DATABASE_URL = os.getenv("DATABASE_URL", "sqlite+aiosqlite:///./ecom_database.db")

engine_args = {
    "echo": False,
    "future": True,
}

if "sqlite" in DATABASE_URL:
    engine_args["connect_args"] = {"check_same_thread": False, "timeout": 30}
else:
    engine_args["poolclass"] = AsyncAdaptedQueuePool
    engine_args["pool_size"] = 25
    engine_args["max_overflow"] = 15
    engine_args["pool_recycle"] = 1800
    engine_args["pool_pre_ping"] = True

engine = create_async_engine(DATABASE_URL, **engine_args)

if "sqlite" in DATABASE_URL:
    @event.listens_for(engine.sync_engine, "connect")
    def set_sqlite_pragma(dbapi_connection, connection_record):
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA journal_mode=WAL")
        cursor.execute("PRAGMA synchronous=NORMAL")
        cursor.close()

AsyncSessionLocal = async_sessionmaker(
    engine,
    class_=AsyncSession,
    expire_on_commit=False,
    autoflush=False
)

class Base(DeclarativeBase):
    pass


class PushTokenModel(Base):
    __tablename__ = "push_tokens"
    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    token: Mapped[str] = mapped_column(String(255), unique=True, index=True, nullable=False)


class ChatModeModel(Base):
    __tablename__ = "chat_modes"
    chat_id: Mapped[str] = mapped_column(String(100), primary_key=True, index=True)
    mode: Mapped[str] = mapped_column(String(20), default="ai", nullable=False)


class StoreCredentialModel(Base):
    __tablename__ = "store_credentials"
    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    shop_domain: Mapped[str] = mapped_column(String(255), unique=True, index=True, nullable=False)
    platform: Mapped[str] = mapped_column(String(50), default="shopify", nullable=False)
    encrypted_credentials: Mapped[str] = mapped_column(Text, nullable=False)


async def init_db() -> None:
    try:
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        logger.info("Таблиці бази даних успішно ініціалізовано та перевірено.")
    except Exception as e:
        logger.error(f"Критична помилка ініціалізації бази даних: {str(e)}", exc_info=True)
        raise


async def close_db() -> None:
    try:
        await engine.dispose()
        logger.info("З'єднання з базою даних успішно закриті.")
    except Exception as e:
        logger.error(f"Помилка при закритті з'єднань з БД: {str(e)}", exc_info=True)


async def get_db() -> AsyncGenerator[AsyncSession, None]:
    """Dependency для FastAPI з авто-комітом, відкатом та захистом від гонки (Atomic)."""
    async with AsyncSessionLocal() as session:
        try:
            yield session
            await session.commit()
        except Exception as e:
            await session.rollback()
            logger.warning(f"Транзакцію скасовано через помилку (Rollback виконано): {str(e)}")
            raise
        finally:
            await session.close()