from __future__ import annotations

from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest

from vpn_access_bot.config import Settings
from vpn_access_bot.db import Database
from vpn_access_bot.handlers.subscription import (
    handle_device_credential,
    handle_device_regenerate_confirm,
    handle_device_regenerate_do,
    handle_device_transfer_confirm,
    handle_device_transfer_do,
    handle_device_transfer_platform,
)
from vpn_access_bot.mediator_client import SubscriptionFeedCredential
from vpn_access_bot.models import Subscription, User, utc_now


class FakeMessage:
    def __init__(self) -> None:
        self.edited_texts: list[str] = []
        self.sent_texts: list[str] = []

    async def edit_text(self, text: str, **kwargs: object) -> None:
        _ = kwargs
        self.edited_texts.append(text)

    async def answer(self, text: str, **kwargs: object) -> None:
        _ = kwargs
        self.sent_texts.append(text)


class FakeCallback:
    def __init__(self, telegram_id: int, data: str) -> None:
        self.from_user = SimpleNamespace(id=telegram_id)
        self.message = FakeMessage()
        self.data = data
        self.answer_count = 0

    async def answer(self, *args: object, **kwargs: object) -> None:
        _ = args, kwargs
        self.answer_count += 1


class SharedOnlyMediator:
    def __init__(self) -> None:
        self.ensure_calls: list[str] = []
        self.personal_calls: list[str] = []

    async def ensure_subscription_feed(self, public_guid: str) -> SubscriptionFeedCredential:
        self.ensure_calls.append(public_guid)
        return SubscriptionFeedCredential(
            status="existing",
            connection_url=(f"https://vpn.example/sub/{public_guid}/feed?token=fake-shared-secret"),
            created=False,
        )

    def __getattr__(self, name: str):
        if name in {
            "get_device_credential",
            "regenerate_device_token",
            "transfer_device_token",
            "create_device_token",
        }:
            self.personal_calls.append(name)
            raise AssertionError(f"Personal credential operation was called: {name}")
        raise AttributeError(name)


async def _create_subscription(database: Database, telegram_id: int) -> str:
    async with database.session() as session:
        now = utc_now()
        user = User(
            telegram_id=telegram_id,
            referral_code=f"shared-{telegram_id}",
            created_at=now,
            updated_at=now,
        )
        session.add(user)
        await session.flush()
        public_guid = f"00000000-0000-0000-0000-{telegram_id:012d}"
        subscription = Subscription(
            user_id=user.id,
            public_guid=public_guid,
            signed_url="",
            max_devices=3,
            status="active",
            starts_at=now,
            expires_at=now + timedelta(days=30),
            created_at=now,
            updated_at_utc=now,
        )
        session.add(subscription)
        await session.flush()
        user.primary_subscription_id = subscription.id
        return public_guid


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("handler", "callback_data"),
    [
        (handle_device_transfer_platform, "device:transfer:legacy-device"),
        (handle_device_transfer_confirm, "device:move:android:legacy-device"),
        (handle_device_transfer_do, "device:move_do:android:legacy-device"),
        (handle_device_credential, "device:credential:legacy-device"),
        (handle_device_regenerate_confirm, "device:regenerate:legacy-device"),
        (handle_device_regenerate_do, "device:regenerate_do:legacy-device"),
    ],
)
async def test_legacy_personal_link_callbacks_return_shared_feed_without_mutation(
    tmp_path: Path,
    handler,
    callback_data: str,
) -> None:
    database = Database(f"sqlite+aiosqlite:///{tmp_path / 'bot.db'}")
    await database.initialize()
    telegram_id = 8201
    public_guid = await _create_subscription(database, telegram_id)
    callback = FakeCallback(telegram_id, callback_data)
    mediator = SharedOnlyMediator()
    settings = Settings(
        TELEGRAM_BOT_TOKEN="test-token",
        MEDIATOR_ADMIN_TOKEN="test-admin-token",
    )

    try:
        await handler(
            callback,  # type: ignore[arg-type]
            database,
            mediator,  # type: ignore[arg-type]
            settings,
        )
    finally:
        await database.dispose()

    assert callback.answer_count == 1
    assert mediator.ensure_calls == [public_guid]
    assert mediator.personal_calls == []
    assert any("одна общая ссылка" in text for text in callback.message.edited_texts)
    assert any("/feed?token=" in text for text in callback.message.sent_texts)
    assert not any("/devices/" in text for text in callback.message.sent_texts)


@pytest.mark.asyncio
async def test_repeated_legacy_callback_is_idempotent_and_returns_same_shared_link(
    tmp_path: Path,
) -> None:
    database = Database(f"sqlite+aiosqlite:///{tmp_path / 'repeat.db'}")
    await database.initialize()
    telegram_id = 8202
    public_guid = await _create_subscription(database, telegram_id)
    mediator = SharedOnlyMediator()
    settings = Settings(
        TELEGRAM_BOT_TOKEN="test-token",
        MEDIATOR_ADMIN_TOKEN="test-admin-token",
    )

    try:
        first = FakeCallback(telegram_id, "device:credential:legacy-device")
        second = FakeCallback(telegram_id, "device:credential:legacy-device")
        await handle_device_credential(
            first,  # type: ignore[arg-type]
            database,
            mediator,  # type: ignore[arg-type]
            settings,
        )
        await handle_device_credential(
            second,  # type: ignore[arg-type]
            database,
            mediator,  # type: ignore[arg-type]
            settings,
        )
    finally:
        await database.dispose()

    assert mediator.ensure_calls == [public_guid, public_guid]
    assert mediator.personal_calls == []
    assert first.message.sent_texts == second.message.sent_texts
