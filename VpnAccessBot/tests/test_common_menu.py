from __future__ import annotations

from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest
from aiogram.types import User as TelegramUser

from vpn_access_bot.commerce import CabinetSubscriptionState
from vpn_access_bot.config import Settings
from vpn_access_bot.db import Database
from vpn_access_bot.handlers.common import _build_main_menu_state
from vpn_access_bot.models import utc_now
from vpn_access_bot.repositories import SubscriptionRepository, UserRepository


class FakeMediatorClient:
    async def get_readiness(self):
        return SimpleNamespace(
            status="ready",
            catalog_state="fresh",
            server_count=2,
            migrations_applied=8,
            migrations_current=True,
            device_issuance_version=2,
        )

    async def get_subscription(self, public_guid: str):
        _ = public_guid
        return SimpleNamespace(
            active_device_count=1,
            max_devices=2,
            is_active=True,
        )


@pytest.mark.asyncio
async def test_main_menu_detects_existing_active_subscription(tmp_path: Path) -> None:
    database = Database(f"sqlite+aiosqlite:///{tmp_path / 'bot.db'}")
    await database.initialize()
    settings = Settings(
        TELEGRAM_BOT_TOKEN="test-token",
        MEDIATOR_ADMIN_TOKEN="test-admin-token",
    )
    telegram_user = TelegramUser(
        id=123456,
        is_bot=False,
        first_name="Misha",
        username="misha",
    )

    try:
        async with database.session() as session:
            user = await UserRepository(session).get_or_create_from_message_user(
                telegram_id=telegram_user.id,
                username=telegram_user.username,
                first_name=telegram_user.first_name,
            )
            await SubscriptionRepository(session).create(
                user=user,
                tariff=None,
                public_guid="00000000-0000-0000-0000-000000000123",
                expires_at=utc_now() + timedelta(days=30),
                max_devices=2,
            )

        state = await _build_main_menu_state(
            telegram_user,
            database,
            settings,
            FakeMediatorClient(),  # type: ignore[arg-type]
        )

        assert state.subscription_state == CabinetSubscriptionState.ACTIVE
        assert state.active_device_tokens == 1
        assert state.max_device_tokens == 2
        assert state.primary_action == "add_device"
    finally:
        await database.dispose()
