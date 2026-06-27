from __future__ import annotations

from datetime import timedelta

import pytest
from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError
from sqlalchemy import select

from vpn_access_bot.config import Settings
from vpn_access_bot.db import Database
from vpn_access_bot.models import NotificationOutbox, User, utc_now
from vpn_access_bot.product_completion import dispatch_notification_outbox_once
from vpn_access_bot.repositories import to_aware_utc


def _settings() -> Settings:
    return Settings(
        TELEGRAM_BOT_TOKEN="test-token",
        MEDIATOR_ADMIN_TOKEN="test-admin-token",
        ADMIN_TELEGRAM_IDS="11",
    )


async def _enqueue_user_notification(
    database: Database,
    *,
    telegram_id: int,
    notification_kind: str = "admin_subscription_adjusted",
    attempt_count: int = 0,
) -> None:
    now = utc_now()
    async with database.session() as session:
        user = User(
            telegram_id=telegram_id,
            referral_code=f"outbox-{telegram_id}",
            created_at=now,
            updated_at=now,
        )
        session.add(user)
        await session.flush()
        session.add(
            NotificationOutbox(
                public_id=f"00000000-0000-0000-0000-{telegram_id:012d}",
                idempotency_key=f"outbox-resilience-{telegram_id}",
                user_id=user.id,
                notification_kind=notification_kind,
                state="pending",
                attempt_count=attempt_count,
                available_at_utc=now,
                created_at_utc=now,
                updated_at_utc=now,
            )
        )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("exception", "expected_code"),
    [
        (
            TelegramForbiddenError(
                method=object(),  # type: ignore[arg-type]
                message="Forbidden: bot was blocked by the user",
            ),
            "telegram_forbidden",
        ),
        (
            TelegramBadRequest(
                method=object(),  # type: ignore[arg-type]
                message="Bad Request: chat not found",
            ),
            "telegram_bad_request",
        ),
    ],
)
async def test_permanent_telegram_rejection_is_terminal_for_user_notifications(
    tmp_path,
    exception: Exception,
    expected_code: str,
) -> None:
    database = Database(f"sqlite+aiosqlite:///{tmp_path / 'bot.db'}")
    await database.initialize()
    await _enqueue_user_notification(database, telegram_id=7001)

    class RejectingSender:
        async def send_message(self, *_args: object, **_kwargs: object):
            raise exception

    try:
        accepted = await dispatch_notification_outbox_once(
            database.session,
            RejectingSender(),  # type: ignore[arg-type]
            _settings(),
        )

        assert accepted == 0
        async with database.session() as session:
            item = (await session.execute(select(NotificationOutbox))).scalar_one()
            assert item.state == "terminal_failed"
            assert item.attempt_count == 1
            assert item.last_error_code == expected_code
    finally:
        await database.dispose()


@pytest.mark.asyncio
async def test_transient_user_notification_failure_has_backoff(tmp_path) -> None:
    database = Database(f"sqlite+aiosqlite:///{tmp_path / 'bot.db'}")
    await database.initialize()
    await _enqueue_user_notification(database, telegram_id=7002)
    before = utc_now()

    class FailingSender:
        async def send_message(self, *_args: object, **_kwargs: object):
            raise RuntimeError("temporary")

    try:
        accepted = await dispatch_notification_outbox_once(
            database.session,
            FailingSender(),  # type: ignore[arg-type]
            _settings(),
        )

        assert accepted == 0
        async with database.session() as session:
            item = (await session.execute(select(NotificationOutbox))).scalar_one()
            assert item.state == "failed"
            assert item.attempt_count == 1
            assert to_aware_utc(item.available_at_utc) >= before + timedelta(seconds=29)
    finally:
        await database.dispose()


@pytest.mark.asyncio
async def test_transient_user_notification_stops_after_retry_limit(tmp_path) -> None:
    database = Database(f"sqlite+aiosqlite:///{tmp_path / 'bot.db'}")
    await database.initialize()
    settings = _settings()
    await _enqueue_user_notification(
        database,
        telegram_id=7003,
        attempt_count=settings.notification_outbox_max_delivery_attempts - 1,
    )

    class FailingSender:
        async def send_message(self, *_args: object, **_kwargs: object):
            raise RuntimeError("temporary")

    try:
        accepted = await dispatch_notification_outbox_once(
            database.session,
            FailingSender(),  # type: ignore[arg-type]
            settings,
        )

        assert accepted == 0
        async with database.session() as session:
            item = (await session.execute(select(NotificationOutbox))).scalar_one()
            assert item.state == "terminal_failed"
            assert item.attempt_count == settings.notification_outbox_max_delivery_attempts
    finally:
        await database.dispose()
