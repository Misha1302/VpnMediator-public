from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace

import pytest
from sqlalchemy import select

from vpn_access_bot.db import Database
from vpn_access_bot.handlers.payments import handle_successful_payment
from vpn_access_bot.models import NotificationOutbox


@pytest.mark.asyncio
async def test_successful_payment_confirmation_is_durable_and_idempotent(tmp_path: Path) -> None:
    database = Database(f"sqlite+aiosqlite:///{tmp_path / 'payment-outbox.db'}")
    await database.initialize()
    message = SimpleNamespace(
        from_user=SimpleNamespace(
            id=7001,
            username="payer",
            first_name="Payer",
        ),
        successful_payment=SimpleNamespace(
            invoice_payload="order:unknown-but-durable",
            total_amount=100,
            currency="XTR",
            telegram_payment_charge_id="charge-7001",
        ),
        date=datetime.now(UTC),
    )
    try:
        await handle_successful_payment(message, database, bot_key="primary")
        await handle_successful_payment(message, database, bot_key="primary")

        async with database.session() as session:
            items = list((await session.execute(select(NotificationOutbox))).scalars().all())

        assert len(items) == 1
        assert items[0].notification_kind == "payment_received"
        assert items[0].state == "pending"
        assert items[0].idempotency_key.startswith("payment-confirmation:")
        assert "charge-7001" not in items[0].idempotency_key
    finally:
        await database.dispose()
