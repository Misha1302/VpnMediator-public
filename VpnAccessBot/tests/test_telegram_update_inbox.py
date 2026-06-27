from __future__ import annotations

from pathlib import Path

import pytest
from sqlalchemy import select

from vpn_access_bot.db import Database
from vpn_access_bot.models import TelegramUpdateInbox
from vpn_access_bot.telegram.update_inbox import TelegramUpdateInboxRepository


@pytest.mark.asyncio
async def test_update_inbox_is_idempotent_and_quarantines_conflicting_redelivery(
    tmp_path: Path,
) -> None:
    database = Database(f"sqlite+aiosqlite:///{tmp_path / 'updates.db'}")
    await database.initialize()
    try:
        async with database.session() as session:
            repository = TelegramUpdateInboxRepository(session)
            first, inserted = await repository.receive(
                bot_key="primary",
                update_id=10,
                payload_json='{"update_id":10}',
            )
            duplicate, duplicate_inserted = await repository.receive(
                bot_key="primary",
                update_id=10,
                payload_json='{"update_id":10}',
            )
            assert first.id == duplicate.id
            assert inserted is True
            assert duplicate_inserted is False

        async with database.session() as session:
            conflict, inserted = await TelegramUpdateInboxRepository(session).receive(
                bot_key="primary",
                update_id=10,
                payload_json='{"update_id":10,"message":{"message_id":1}}',
            )
            assert inserted is False
            assert conflict.status == "quarantined"
            assert conflict.failure_code == "update_payload_conflict"

        async with database.session() as session:
            rows = (await session.execute(select(TelegramUpdateInbox))).scalars().all()
            assert len(rows) == 1
    finally:
        await database.dispose()


@pytest.mark.asyncio
async def test_update_inbox_retries_then_quarantines_poison_update(tmp_path: Path) -> None:
    database = Database(f"sqlite+aiosqlite:///{tmp_path / 'poison.db'}")
    await database.initialize()
    try:
        async with database.session() as session:
            item, _ = await TelegramUpdateInboxRepository(session).receive(
                bot_key="primary",
                update_id=11,
                payload_json='{"update_id":11}',
            )
            item_id = item.id

        async with database.session() as session:
            claimed = await TelegramUpdateInboxRepository(session).claim_due(
                worker_id="worker",
                limit=1,
                lease_seconds=30,
            )
            assert len(claimed) == 1
            status = await TelegramUpdateInboxRepository(session).mark_failed(
                item_id,
                worker_id="worker",
                failure_code="handler_failed",
                error_message="boom",
                max_attempts=1,
                retry_delay_seconds=1,
            )
            assert status == "quarantined"

        async with database.session() as session:
            item = await session.get(TelegramUpdateInbox, item_id)
            assert item is not None
            assert item.status == "quarantined"
            assert item.attempt_count == 1
            assert item.claimed_by is None
    finally:
        await database.dispose()


@pytest.mark.asyncio
async def test_update_inbox_reclaims_expired_processing_lease(tmp_path: Path) -> None:
    database = Database(f"sqlite+aiosqlite:///{tmp_path / 'expired-lease.db'}")
    await database.initialize()
    try:
        async with database.session() as session:
            await TelegramUpdateInboxRepository(session).receive(
                bot_key="primary",
                update_id=12,
                payload_json='{"update_id":12}',
            )

        async with database.session() as session:
            first_claim = await TelegramUpdateInboxRepository(session).claim_due(
                worker_id="worker-one",
                limit=1,
                lease_seconds=30,
            )
            assert len(first_claim) == 1
            item_id = first_claim[0].id
            first_attempt_count = first_claim[0].attempt_count

        async with database.session() as session:
            item = await session.get(TelegramUpdateInbox, item_id)
            assert item is not None
            item.claim_expires_at_utc = item.received_at_utc

        async with database.session() as session:
            second_claim = await TelegramUpdateInboxRepository(session).claim_due(
                worker_id="worker-two",
                limit=1,
                lease_seconds=30,
            )
            assert len(second_claim) == 1
            assert second_claim[0].id == item_id
            assert second_claim[0].claimed_by == "worker-two"
            assert second_claim[0].attempt_count == first_attempt_count + 1
    finally:
        await database.dispose()
