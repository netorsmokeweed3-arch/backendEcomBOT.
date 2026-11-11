"""Модуль бази даних для SaaS-платформи (OmniFlow Enterprise - Multi-Tenancy & Audit Logs).
Асинхронний SQLAlchemy рушій із підтримкою користувачів, ізоляцією даних, WAL-режимом
та вбудованим шифруванням чутливих даних у спокої (Encryption at Rest).
"""

import os
import logging
from typing import AsyncGenerator
from sqlalchemy.ext.asyncio import create_async_engine, AsyncSession, async_sessionmaker
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column
from sqlalchemy import String, Text, event, select, ForeignKey, DateTime
from sqlalchemy.pool import AsyncAdaptedQueuePool
from cryptography.fernet import Fernet
from datetime import datetime

logger = logging.getLogger(__name__)

# --- КРИПТОГРАФІЧНИЙ ЗАХИСТ (Fail-Fast для Encryption Key) ---
ENCRYPTION_KEY = os.getenv("APP_ENCRYPTION_KEY")
if not ENCRYPTION_KEY:
    raise RuntimeError(
        "КРИТИЧНА ПОМИЛКА БЕЗПЕКИ: Змінна середовища APP_ENCRYPTION_KEY не задана! "
        "Шифрування даних у спокої вимагає постійного ключа."
    )

cipher_suite = Fernet(ENCRYPTION_KEY.encode("utf-8") if isinstance(ENCRYPTION_KEY, str) else ENCRYPTION_KEY)

def encrypt_credential(plain_text: str) -> str:
    """Шифрує рядок перед збереженням у базу даних."""
    if not plain_text:
        return ""
    return cipher_suite.encrypt(plain_text.encode("utf-8")).decode("utf-8")

def decrypt_credential(encrypted_text: str) -> str:
    """Розшифровує рядок із бази даних."""
    if not encrypted_text:
        return ""
    try:
        return cipher_suite.decrypt(encrypted_text.encode("utf-8")).decode("utf-8")
    except Exception as e:
        logger.critical(f"КРИТИЧНА ПОМИЛКА ШИФРУВАННЯ БД: Неможливо розшифрувати дані. Деталі: {e}")
        raise

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


class UserModel(Base):
    __tablename__ = "users"
    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    email: Mapped[str] = mapped_column(String(255), unique=True, index=True, nullable=False)
    hashed_password: Mapped[str] = mapped_column(String(255), nullable=False)


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
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True, nullable=False)
    shop_domain: Mapped[str] = mapped_column(String(255), unique=True, index=True, nullable=False)
    platform: Mapped[str] = mapped_column(String(50), default="shopify", nullable=False)
    encrypted_credentials: Mapped[str] = mapped_column(Text, nullable=False)


class AuditLogModel(Base):
    """Таблиця для логування помилок синхронізації з маркетплейсами."""
    __tablename__ = "audit_logs"
    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True, nullable=False)
    shop_domain: Mapped[str] = mapped_column(String(255), index=True, nullable=False)
    event_type: Mapped[str] = mapped_column(String(50), nullable=False)  # напр. "SYNC_ERROR", "SUCCESS"
    message: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)


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