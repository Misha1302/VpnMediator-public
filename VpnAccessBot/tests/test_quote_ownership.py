from __future__ import annotations

from datetime import timedelta
from pathlib import Path

import pytest

from vpn_access_bot.config import Settings
from vpn_access_bot.db import Database
from vpn_access_bot.models import utc_now
from vpn_access_bot.product_catalog import ProductCatalog
from vpn_access_bot.repositories import PurchaseQuoteRepository, UserRepository
from vpn_access_bot.services import PurchaseService


class UnusedMediatorClient:
    pass


@pytest.mark.asyncio
async def test_foreign_actor_does_not_consume_quote(tmp_path: Path) -> None:
    database = Database(f"sqlite+aiosqlite:///{tmp_path / 'ownership.db'}")
    await database.initialize()
    settings = Settings(
        TELEGRAM_BOT_TOKEN="test-token",
        PAYMENT_MODE="telegram_stars",
        MEDIATOR_ADMIN_TOKEN="test-admin-token",
    )
    pricing_identity = ProductCatalog.from_settings(settings).pricing_identity

    try:
        async with database.session() as session:
            users = UserRepository(session)
            owner = await users.get_or_create_from_message_user(100, "owner", "Owner")
            await users.get_or_create_from_message_user(200, "foreign", "Foreign")
            quote = await PurchaseQuoteRepository(session).create(
                user=owner,
                period_count=1,
                duration_days=30,
                max_devices=1,
                amount_minor_units=60,
                currency="XTR",
                pricing_version=pricing_identity,
                target_subscription_id=None,
                order_kind="purchase",
                expires_at=utc_now() + timedelta(minutes=20),
            )
            public_quote_id = quote.public_quote_id

        async with database.session() as session:
            service = PurchaseService(
                session,
                settings,
                UnusedMediatorClient(),  # type: ignore[arg-type]
            )
            with pytest.raises(ValueError, match="Quote was not found"):
                await service.create_order_from_quote(public_quote_id, actor_telegram_id=200)

        async with database.session() as session:
            quote = await PurchaseQuoteRepository(session).get_by_public_id(public_quote_id)
            assert quote is not None
            assert quote.consumed_at_utc is None

        async with database.session() as session:
            service = PurchaseService(
                session,
                settings,
                UnusedMediatorClient(),  # type: ignore[arg-type]
            )
            order = await service.create_order_from_quote(public_quote_id, actor_telegram_id=100)
            assert order.user_id is not None

        async with database.session() as session:
            quote = await PurchaseQuoteRepository(session).get_by_public_id(public_quote_id)
            assert quote is not None
            assert quote.consumed_at_utc is not None
    finally:
        await database.dispose()


@pytest.mark.asyncio
async def test_concurrent_owner_callbacks_create_exactly_one_order(tmp_path: Path) -> None:
    import asyncio

    from sqlalchemy import text

    database = Database(f"sqlite+aiosqlite:///{tmp_path / 'quote-race.db'}")
    await database.initialize()
    settings = Settings(
        TELEGRAM_BOT_TOKEN="test-token",
        PAYMENT_MODE="telegram_stars",
        MEDIATOR_ADMIN_TOKEN="test-admin-token",
    )
    pricing_identity = ProductCatalog.from_settings(settings).pricing_identity

    try:
        async with database.session() as session:
            owner = await UserRepository(session).get_or_create_from_message_user(
                301, "race-owner", "Race"
            )
            quote = await PurchaseQuoteRepository(session).create(
                user=owner,
                period_count=1,
                duration_days=30,
                max_devices=1,
                amount_minor_units=60,
                currency="XTR",
                pricing_version=pricing_identity,
                target_subscription_id=None,
                order_kind="purchase",
                expires_at=utc_now() + timedelta(minutes=20),
            )
            public_quote_id = quote.public_quote_id

        async def consume() -> int:
            async with database.session() as session:
                order = await PurchaseService(
                    session,
                    settings,
                    UnusedMediatorClient(),  # type: ignore[arg-type]
                ).create_order_from_quote(public_quote_id, actor_telegram_id=301)
                return order.id

        order_ids = await asyncio.gather(consume(), consume())

        async with database.session() as session:
            count = await session.execute(
                text(
                    "SELECT COUNT(*) FROM orders WHERE quote_id = "
                    "(SELECT id FROM purchase_quotes WHERE public_quote_id = :public_quote_id)"
                ),
                {"public_quote_id": public_quote_id},
            )

        assert len(set(order_ids)) == 1
        assert count.scalar_one() == 1
    finally:
        await database.dispose()


@pytest.mark.asyncio
async def test_stars_and_sbp_race_can_select_only_one_provider(tmp_path: Path) -> None:
    import asyncio

    from sqlalchemy import text

    database = Database(f"sqlite+aiosqlite:///{tmp_path / 'provider-race.db'}")
    await database.initialize()
    settings = Settings(
        TELEGRAM_BOT_TOKEN="test-token",
        PAYMENT_MODE="telegram_stars",
        MEDIATOR_ADMIN_TOKEN="test-admin-token",
        EXTERNAL_PAYMENT_ENABLED=True,
        YOOKASSA_INTEGRATION_ENABLED=True,
        CHECKOUT_PUBLIC_BASE_URL="https://pay.example.test",
        CHECKOUT_TOKEN_SECRET="c" * 40,
        YOOKASSA_SHOP_ID="123456",
        YOOKASSA_SECRET_KEY="y" * 40,
        YOOKASSA_RETURN_URL="https://pay.example.test/return",
        YOOKASSA_WEBHOOK_PATH_SECRET="w" * 32,
    )
    pricing_identity = ProductCatalog.from_settings(settings).pricing_identity

    try:
        async with database.session() as session:
            owner = await UserRepository(session).get_or_create_from_message_user(
                401, "provider-race", "Race"
            )
            quote = await PurchaseQuoteRepository(session).create(
                user=owner,
                period_count=1,
                duration_days=30,
                max_devices=1,
                amount_minor_units=60,
                currency="XTR",
                pricing_version=pricing_identity,
                target_subscription_id=None,
                order_kind="purchase",
                expires_at=utc_now() + timedelta(minutes=20),
            )
            public_quote_id = quote.public_quote_id

        async def consume(provider: str) -> int:
            async with database.session() as session:
                order = await PurchaseService(
                    session,
                    settings,
                    UnusedMediatorClient(),  # type: ignore[arg-type]
                ).create_order_from_quote(
                    public_quote_id,
                    actor_telegram_id=401,
                    payment_provider=provider,
                )
                return order.id

        results = await asyncio.gather(
            consume("telegram_stars"),
            consume("yookassa_sbp"),
            return_exceptions=True,
        )

        assert sum(isinstance(result, int) for result in results) == 1
        assert sum(isinstance(result, ValueError) for result in results) == 1
        async with database.session() as session:
            count = await session.execute(
                text(
                    "SELECT COUNT(*) FROM orders WHERE quote_id = "
                    "(SELECT id FROM purchase_quotes WHERE public_quote_id = :quote_id)"
                ),
                {"quote_id": public_quote_id},
            )
            assert count.scalar_one() == 1
    finally:
        await database.dispose()
