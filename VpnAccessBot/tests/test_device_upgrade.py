from __future__ import annotations

from datetime import timedelta
from pathlib import Path

import pytest

from vpn_access_bot.config import Settings
from vpn_access_bot.constants import ORDER_KIND_UPGRADE_DEVICES
from vpn_access_bot.db import Database
from vpn_access_bot.models import (
    CommercialEntitlementSegment,
    Order,
    OrderApplication,
    Subscription,
    User,
    UserDiscount,
    utc_now,
)
from vpn_access_bot.services import PurchaseService


class UnusedMediatorClient:
    pass


def make_settings() -> Settings:
    return Settings(
        TELEGRAM_BOT_TOKEN="test-token",
        PAYMENT_MODE="telegram_stars",
        MEDIATOR_ADMIN_TOKEN="test-admin-token",
        PRICING_BASE_DEVICE_MONTH_STARS=100,
    )


@pytest.mark.asyncio
async def test_complimentary_purchase_can_upgrade_device_limit(tmp_path: Path) -> None:
    database = Database(f"sqlite+aiosqlite:///{tmp_path / 'complimentary-upgrade.db'}")
    await database.initialize()

    try:
        async with database.session() as session:
            now = utc_now()
            user = User(
                telegram_id=1001,
                referral_code="complimentary-upgrade-user",
                created_at=now,
                updated_at=now,
            )
            session.add(user)
            await session.flush()

            subscription = Subscription(
                user_id=user.id,
                public_guid="00000000-0000-0000-0000-000000001001",
                signed_url="",
                max_devices=1,
                status="active",
                starts_at=now,
                expires_at=now + timedelta(days=30),
                created_at=now,
                updated_at_utc=now,
            )
            session.add(subscription)
            await session.flush()
            user.primary_subscription_id = subscription.id

            discount = UserDiscount(
                user_id=user.id,
                discount_bps=10_000,
                scope="purchase",
                max_uses=1,
                used_count=1,
                status="active",
                reason="Complimentary subscription",
                created_by_admin_telegram_id=999,
                created_at_utc=now,
            )
            session.add(discount)
            await session.flush()

            order = Order(
                user_id=user.id,
                status="paid",
                period_count=1,
                duration_days=30,
                selected_max_devices=1,
                amount_minor_units=0,
                final_amount_minor_units=0,
                price_before_personal_discount=100,
                personal_discount_id=discount.id,
                personal_discount_bps=10_000,
                personal_discount_amount_minor_units=100,
                currency="XTR",
                provider="telegram_stars",
                pricing_version="test",
                order_kind="purchase",
                invoice_payload="complimentary-upgrade-order",
                public_order_id="complimentary-upgrade-public-order",
                referral_eligible=False,
                created_at=now,
                completed_at_utc=now,
            )
            session.add(order)
            await session.flush()

            session.add(
                OrderApplication(
                    order_id=order.id,
                    subscription_id=subscription.id,
                    applied_at_utc=now,
                    duration_days=30,
                    selected_max_devices=1,
                    resulting_valid_until_utc=subscription.expires_at,
                    resulting_entitlement_version=1,
                )
            )
            session.add(
                CommercialEntitlementSegment(
                    subscription_id=subscription.id,
                    source_kind="complimentary_order",
                    starts_at_utc=now,
                    ends_at_utc=subscription.expires_at,
                    source_order_id=order.id,
                    source_entity_id=str(order.id),
                    idempotency_key="complimentary-upgrade-segment",
                    status="applied",
                    created_at_utc=now,
                )
            )
            await session.flush()

            service = PurchaseService(
                session,
                make_settings(),
                UnusedMediatorClient(),  # type: ignore[arg-type]
            )
            quote = await service.create_quote(
                telegram_id=user.telegram_id,
                username=None,
                first_name=None,
                period_count=0,
                max_devices=12,
                order_kind=ORDER_KIND_UPGRADE_DEVICES,
                target_subscription_id=subscription.id,
            )

            assert quote.max_devices == 12
            assert quote.target_subscription_id == subscription.id
            assert quote.upgrade_amount_minor_units == 1100
            assert quote.amount_minor_units == 1100
            assert quote.personal_discount_bps == 0

            upgrade_order = await service.create_order_from_quote(
                quote.public_quote_id,
                actor_telegram_id=user.telegram_id,
            )

            assert upgrade_order.order_kind == ORDER_KIND_UPGRADE_DEVICES
            assert upgrade_order.duration_days == 0
            assert upgrade_order.selected_max_devices == 12
            assert upgrade_order.amount_minor_units == 1100
    finally:
        await database.dispose()


@pytest.mark.asyncio
async def test_trial_only_subscription_cannot_upgrade_device_limit(tmp_path: Path) -> None:
    database = Database(f"sqlite+aiosqlite:///{tmp_path / 'trial-upgrade.db'}")
    await database.initialize()

    try:
        async with database.session() as session:
            now = utc_now()
            user = User(
                telegram_id=1002,
                referral_code="trial-upgrade-user",
                created_at=now,
                updated_at=now,
            )
            session.add(user)
            await session.flush()

            subscription = Subscription(
                user_id=user.id,
                public_guid="00000000-0000-0000-0000-000000001002",
                signed_url="",
                max_devices=1,
                status="active",
                starts_at=now,
                expires_at=now + timedelta(days=2),
                created_at=now,
                updated_at_utc=now,
            )
            session.add(subscription)
            await session.flush()
            user.primary_subscription_id = subscription.id
            session.add(
                CommercialEntitlementSegment(
                    subscription_id=subscription.id,
                    source_kind="trial",
                    starts_at_utc=now,
                    ends_at_utc=subscription.expires_at,
                    source_entity_id="trial-claim",
                    idempotency_key="trial-upgrade-segment",
                    status="applied",
                    created_at_utc=now,
                )
            )
            await session.flush()

            service = PurchaseService(
                session,
                make_settings(),
                UnusedMediatorClient(),  # type: ignore[arg-type]
            )
            with pytest.raises(ValueError, match="paid_access_required_for_device_upgrade"):
                await service.create_quote(
                    telegram_id=user.telegram_id,
                    username=None,
                    first_name=None,
                    period_count=0,
                    max_devices=2,
                    order_kind=ORDER_KIND_UPGRADE_DEVICES,
                    target_subscription_id=subscription.id,
                )
    finally:
        await database.dispose()
