from __future__ import annotations

import sqlite3
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, date, datetime
from pathlib import Path

from sqlalchemy import event, select
from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from vpn_access_bot.migrations import run_migrations
from vpn_access_bot.models import Tariff


def _adapt_sqlite_date(value: date) -> str:
    return value.isoformat()


def _adapt_sqlite_datetime(value: datetime) -> str:
    normalized = value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)
    return normalized.isoformat(sep=" ")


def _register_sqlite_adapters() -> None:
    sqlite3.register_adapter(date, _adapt_sqlite_date)
    sqlite3.register_adapter(datetime, _adapt_sqlite_datetime)


_register_sqlite_adapters()


DEFAULT_TARIFFS = [
    {
        "code": "month_3_devices",
        "title": "1 месяц",
        "description": "Доступ на 30 дней, до 3 устройств.",
        "price_minor_units": 199,
        "currency": "XTR",
        "duration_days": 30,
        "max_devices": 3,
        "sort_order": 10,
    },
    {
        "code": "quarter_3_devices",
        "title": "3 месяца",
        "description": "Доступ на 90 дней, до 3 устройств.",
        "price_minor_units": 499,
        "currency": "XTR",
        "duration_days": 90,
        "max_devices": 3,
        "sort_order": 20,
    },
    {
        "code": "family_month_6_devices",
        "title": "Family 1 месяц",
        "description": "Доступ на 30 дней, до 6 устройств.",
        "price_minor_units": 349,
        "currency": "XTR",
        "duration_days": 30,
        "max_devices": 6,
        "sort_order": 30,
    },
]


class Database:
    def __init__(self, database_url: str) -> None:
        self._database_url = database_url
        self.engine = create_async_engine(database_url, echo=False)
        event.listen(self.engine.sync_engine, "connect", self._configure_sqlite_connection)
        self.session_factory = async_sessionmaker(
            self.engine,
            expire_on_commit=False,
            autoflush=False,
        )

    async def initialize(self) -> None:
        self._ensure_sqlite_directory_exists()

        async with self.engine.begin() as connection:
            await run_migrations(connection)

        await self._seed_tariffs()

    @asynccontextmanager
    async def session(self) -> AsyncIterator[AsyncSession]:
        async with self.session_factory() as session:
            try:
                yield session
                await session.commit()
            except Exception:
                await session.rollback()
                raise

    async def dispose(self) -> None:
        await self.engine.dispose()

    def _ensure_sqlite_directory_exists(self) -> None:
        prefix = "sqlite+aiosqlite:///"

        if not self._database_url.startswith(prefix):
            return

        raw_path = self._database_url.removeprefix(prefix)

        if raw_path.startswith(":memory:"):
            return

        path = Path(raw_path)

        if path.parent != Path(""):
            path.parent.mkdir(parents=True, exist_ok=True)

    async def _seed_tariffs(self) -> None:
        async with self.session() as session:
            for tariff_data in DEFAULT_TARIFFS:
                result = await session.execute(
                    select(Tariff).where(Tariff.code == tariff_data["code"]),
                )
                existing = result.scalar_one_or_none()

                if existing is None:
                    session.add(Tariff(**tariff_data))
                    continue

                existing.title = str(tariff_data["title"])
                existing.description = str(tariff_data["description"])
                existing.price_minor_units = int(tariff_data["price_minor_units"])
                existing.currency = str(tariff_data["currency"])
                existing.duration_days = int(tariff_data["duration_days"])
                existing.max_devices = int(tariff_data["max_devices"])
                existing.sort_order = int(tariff_data["sort_order"])
                existing.is_active = True

    @staticmethod
    def _configure_sqlite_connection(dbapi_connection, connection_record) -> None:
        _ = connection_record
        cursor = dbapi_connection.cursor()
        try:
            cursor.execute("PRAGMA foreign_keys=ON")
            cursor.execute("PRAGMA journal_mode=WAL")
            cursor.execute("PRAGMA synchronous=NORMAL")
            cursor.execute("PRAGMA busy_timeout=30000")
        finally:
            cursor.close()
