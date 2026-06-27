from datetime import timedelta
from pathlib import Path

import pytest
from sqlalchemy import select

from vpn_access_bot.db import Database
from vpn_access_bot.models import (
    BroadcastCampaign,
    BroadcastRecipient,
    NotificationOutbox,
    ProductEvent,
    PurchaseQuote,
    TelegramUpdateInbox,
    User,
    utc_now,
)
from vpn_access_bot.retention import cleanup_once


@pytest.mark.asyncio
async def test_cleanup_dry_run_and_apply_preserve_financial_rows(tmp_path: Path) -> None:
    database = Database(f"sqlite+aiosqlite:///{tmp_path / 'bot.db'}")
    await database.initialize()
    old = utc_now() - timedelta(days=120)
    try:
        async with database.session() as session:
            user = User(
                telegram_id=9090,
                username=None,
                first_name="Retention",
                created_at=old,
                updated_at=old,
            )
            session.add(user)
            await session.flush()
            session.add(
                PurchaseQuote(
                    user_id=user.id,
                    period_count=1,
                    duration_days=30,
                    max_devices=1,
                    amount_minor_units=10,
                    currency="XTR",
                    pricing_version="test",
                    order_kind="purchase",
                    expires_at_utc=old,
                    created_at_utc=old,
                )
            )
            session.add(
                ProductEvent(
                    user_id=user.id,
                    event_name="old_event",
                    occurred_at_utc=old,
                )
            )

        async with database.session() as session:
            dry = await cleanup_once(session, retention_days=90, dry_run=True)
            assert dry.expired_quotes == 1
            assert dry.product_events == 1

        async with database.session() as session:
            assert (await session.execute(select(PurchaseQuote))).scalars().all()
            result = await cleanup_once(session, retention_days=90, dry_run=False)
            assert result.expired_quotes == 1
            assert result.product_events == 1

        async with database.session() as session:
            assert not (await session.execute(select(PurchaseQuote))).scalars().all()
            assert not (await session.execute(select(ProductEvent))).scalars().all()
    finally:
        await database.dispose()


@pytest.mark.asyncio
async def test_cleanup_removes_completed_broadcast_payload_after_retention(tmp_path: Path) -> None:
    database = Database(f"sqlite+aiosqlite:///{tmp_path / 'broadcast.db'}")
    await database.initialize()
    old = utc_now() - timedelta(days=120)
    try:
        async with database.session() as session:
            user = User(
                telegram_id=9191,
                username=None,
                first_name="Broadcast",
                referral_code="broadcast-retention",
                created_at=old,
                updated_at=old,
            )
            session.add(user)
            await session.flush()
            campaign = BroadcastCampaign(
                public_id="00000000-0000-0000-0000-000000009191",
                admin_telegram_id=11,
                source_bot_key="default",
                source_chat_id=11,
                source_message_id=91,
                filter_kind="all",
                recipient_pattern=None,
                recipient_upper_bound_user_id=user.id,
                message_text="Старый текст рассылки",
                message_sha256="a" * 64,
                confirmation_token_hash="b" * 64,
                state="queued",
                target_count=1,
                queued_count=1,
                expires_at_utc=old,
                confirmed_at_utc=old,
                queued_at_utc=old,
                created_at_utc=old,
                updated_at_utc=old,
            )
            session.add(campaign)
            await session.flush()
            session.add(
                BroadcastRecipient(
                    campaign_id=campaign.id,
                    user_id=user.id,
                    telegram_id=user.telegram_id,
                    created_at_utc=old,
                )
            )
            session.add(
                NotificationOutbox(
                    public_id="00000000-0000-0000-0000-000000019191",
                    idempotency_key="old-broadcast-outbox",
                    user_id=user.id,
                    broadcast_campaign_id=campaign.id,
                    notification_kind="admin_broadcast",
                    payload_json='{"campaign_id":"00000000-0000-0000-0000-000000009191"}',
                    state="provider_accepted",
                    attempt_count=1,
                    available_at_utc=old,
                    provider_accepted_at_utc=old,
                    created_at_utc=old,
                    updated_at_utc=old,
                )
            )

        async with database.session() as session:
            dry = await cleanup_once(session, retention_days=90, dry_run=True)
            assert dry.notification_outbox == 1
            assert dry.broadcast_recipients == 0
            assert dry.broadcast_campaigns == 0

        async with database.session() as session:
            result = await cleanup_once(session, retention_days=90, dry_run=False)
            assert result.notification_outbox == 1
            assert result.broadcast_recipients == 1
            assert result.broadcast_campaigns == 1

        async with database.session() as session:
            assert not (await session.execute(select(NotificationOutbox))).scalars().all()
            assert not (await session.execute(select(BroadcastRecipient))).scalars().all()
            assert not (await session.execute(select(BroadcastCampaign))).scalars().all()
    finally:
        await database.dispose()


@pytest.mark.asyncio
async def test_cleanup_removes_terminal_telegram_updates_after_retention(
    tmp_path: Path,
) -> None:
    database = Database(f"sqlite+aiosqlite:///{tmp_path / 'telegram-retention.db'}")
    await database.initialize()
    old = utc_now() - timedelta(days=120)
    try:
        async with database.session() as session:
            session.add(
                TelegramUpdateInbox(
                    bot_key="primary",
                    update_id=123,
                    payload_hash="a" * 64,
                    payload_json='{"message":{"text":"private"}}',
                    status="quarantined",
                    attempt_count=8,
                    received_at_utc=old,
                    processed_at_utc=old,
                    failure_code="handler_failed",
                    last_error_message="token=[REDACTED]",
                    updated_at_utc=old,
                )
            )

        async with database.session() as session:
            dry = await cleanup_once(session, retention_days=90, dry_run=True)
            assert dry.telegram_update_inbox == 1

        async with database.session() as session:
            result = await cleanup_once(session, retention_days=90, dry_run=False)
            assert result.telegram_update_inbox == 1

        async with database.session() as session:
            assert not (await session.execute(select(TelegramUpdateInbox))).scalars().all()
    finally:
        await database.dispose()
