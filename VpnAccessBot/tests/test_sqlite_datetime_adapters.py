from __future__ import annotations

import sqlite3
from datetime import UTC, date, datetime, timedelta, timezone

from vpn_access_bot.db import _adapt_sqlite_date, _adapt_sqlite_datetime


def test_date_adapter_uses_iso_8601() -> None:
    assert _adapt_sqlite_date(date(2026, 6, 12)) == "2026-06-12"


def test_datetime_adapter_normalizes_aware_values_to_utc() -> None:
    source = datetime(2026, 6, 12, 15, 30, 45, 123456, tzinfo=timezone(timedelta(hours=3)))

    assert _adapt_sqlite_datetime(source) == "2026-06-12 12:30:45.123456+00:00"


def test_datetime_adapter_treats_legacy_naive_values_as_utc() -> None:
    source = datetime(2026, 6, 12, 12, 30, 45, 123456)

    assert _adapt_sqlite_datetime(source) == "2026-06-12 12:30:45.123456+00:00"


def test_registered_datetime_adapter_does_not_use_deprecated_default() -> None:
    connection = sqlite3.connect(":memory:")
    try:
        connection.execute("CREATE TABLE values_table(value TEXT NOT NULL)")
        connection.execute(
            "INSERT INTO values_table(value) VALUES (?)",
            (datetime(2026, 6, 12, 12, 30, tzinfo=UTC),),
        )
        stored = connection.execute("SELECT value FROM values_table").fetchone()[0]
    finally:
        connection.close()

    assert stored == "2026-06-12 12:30:00+00:00"
