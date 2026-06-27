from __future__ import annotations

from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest
from aiogram.enums import ChatType

from vpn_access_bot.config import Settings
from vpn_access_bot.db import Database
from vpn_access_bot.handlers.onboarding import handle_credential_create
from vpn_access_bot.mediator_client import SubscriptionFeedCredential
from vpn_access_bot.models import OnboardingSession, Subscription, User, utc_now
from vpn_access_bot.repositories import OnboardingSessionRepository


async def _create_onboarding(database: Database, telegram_id: int) -> tuple[int, str]:
    async with database.session() as session:
        now = utc_now()
        user = User(
            telegram_id=telegram_id,
            referral_code=f"retry-{telegram_id}",
            created_at=now,
            updated_at=now,
        )
        session.add(user)
        await session.flush()
        subscription = Subscription(
            user_id=user.id,
            public_guid=f"00000000-0000-0000-0000-{telegram_id:012d}",
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
        onboarding = await OnboardingSessionRepository(session).start_or_update(
            user=user,
            subscription=subscription,
            platform="android",
            current_step="waiting_first_fetch",
            status="waiting_first_fetch",
        )
        onboarding.device_public_id = "revoked-device"
        onboarding.handoff_claim_id = None
        assert onboarding.issuance_request_id is not None
        return onboarding.id, onboarding.issuance_request_id


@pytest.mark.asyncio
async def test_concurrent_retry_requests_converge_on_one_new_key(tmp_path: Path) -> None:
    database = Database(f"sqlite+aiosqlite:///{tmp_path / 'converge.db'}")
    await database.initialize()

    try:
        onboarding_id, original_request_id = await _create_onboarding(database, 8102)

        async with database.session() as session:
            first_request_id, first_restarted = await OnboardingSessionRepository(
                session
            ).restart_device_issuance(
                onboarding_id,
                original_request_id,
            )

        async with database.session() as session:
            second_request_id, second_restarted = await OnboardingSessionRepository(
                session
            ).restart_device_issuance(
                onboarding_id,
                original_request_id,
            )

        assert first_restarted is True
        assert second_restarted is False
        assert second_request_id == first_request_id
        assert first_request_id != original_request_id
    finally:
        await database.dispose()


class FakeMessage:
    def __init__(self) -> None:
        self.chat = SimpleNamespace(type=ChatType.PRIVATE)
        self.edited_texts: list[str] = []
        self.sent_texts: list[str] = []

    async def edit_text(self, text: str, **kwargs: object) -> None:
        _ = kwargs
        self.edited_texts.append(text)

    async def answer(self, text: str, **kwargs: object) -> None:
        _ = kwargs
        self.sent_texts.append(text)


class FakeCallback:
    def __init__(self, telegram_id: int) -> None:
        self.from_user = SimpleNamespace(
            id=telegram_id,
            username=None,
            first_name="User",
        )
        self.message = FakeMessage()
        self.answer_count = 0

    async def answer(self, *args: object, **kwargs: object) -> None:
        _ = args, kwargs
        self.answer_count += 1


class ReadyService:
    async def check(self, *, force: bool = False, operation_kind=None) -> SimpleNamespace:
        _ = force, operation_kind
        return SimpleNamespace(can_sell=True, reason_code=None)


class UnifiedFeedMediator:
    def __init__(self) -> None:
        self.subscription_guids: list[str] = []

    async def ensure_subscription_feed(self, public_guid: str) -> SubscriptionFeedCredential:
        self.subscription_guids.append(public_guid)
        return SubscriptionFeedCredential(
            status="active",
            connection_url="https://vpn.example/sub/test/feed?token=fake-secret",
            created=True,
        )


@pytest.mark.asyncio
async def test_retry_button_delivers_unified_subscription_link(tmp_path: Path) -> None:
    database = Database(f"sqlite+aiosqlite:///{tmp_path / 'handler-retry.db'}")
    await database.initialize()

    try:
        onboarding_id, original_request_id = await _create_onboarding(database, 8104)
        callback = FakeCallback(8104)
        mediator = UnifiedFeedMediator()
        settings = Settings(
            TELEGRAM_BOT_TOKEN="test-token",
            MEDIATOR_ADMIN_TOKEN="test-admin-token",
        )

        await handle_credential_create(
            callback,  # type: ignore[arg-type]
            database,
            mediator,  # type: ignore[arg-type]
            ReadyService(),  # type: ignore[arg-type]
            settings,
        )

        async with database.session() as session:
            onboarding = await session.get(OnboardingSession, onboarding_id)
            assert onboarding is not None
            assert onboarding.issuance_request_id == original_request_id
            assert onboarding.device_public_id is None
            assert onboarding.status == "waiting_first_fetch"

        assert callback.answer_count == 1
        assert any("Ссылка подписки готова" in text for text in callback.message.edited_texts)
        assert any(
            "https://vpn.example/sub/test/feed?token=fake-secret" in text
            for text in callback.message.sent_texts
        )
        assert mediator.subscription_guids == ["00000000-0000-0000-0000-000000008104"]
    finally:
        await database.dispose()
