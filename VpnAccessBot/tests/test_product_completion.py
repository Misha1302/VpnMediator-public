from __future__ import annotations

from datetime import timedelta

import pytest
from sqlalchemy import select

from vpn_access_bot.db import Database
from vpn_access_bot.models import (
    CommercialEntitlementSegment,
    DiscountRedemption,
    NotificationDelivery,
    Order,
    Subscription,
    User,
    UserDiscount,
    utc_now,
)
from vpn_access_bot.product_completion import (
    bind_referrer_from_payload,
    record_entitlement_segment_once,
    release_discount_for_order,
    remaining_paid_seconds,
    reserve_discount_for_order,
)


@pytest.mark.asyncio
async def test_referrer_binding_is_single_level_and_rejects_self(tmp_path) -> None:
    database = Database(f"sqlite+aiosqlite:///{tmp_path / 'bot.db'}")
    await database.initialize()

    try:
        async with database.session() as session:
            referrer = User(
                telegram_id=1,
                username="referrer",
                first_name="Referrer",
                referral_code="ref-code",
                created_at=utc_now(),
                updated_at=utc_now(),
            )
            referred = User(
                telegram_id=2,
                username="referred",
                first_name="Referred",
                referral_code="own-code",
                created_at=utc_now(),
                updated_at=utc_now(),
            )
            session.add_all([referrer, referred])
            await session.flush()

            assert await bind_referrer_from_payload(session, referred, "ref_ref-code") is True
            assert referred.referred_by_user_id == referrer.id
            assert await bind_referrer_from_payload(session, referred, "ref_own-code") is False
    finally:
        await database.dispose()


@pytest.mark.asyncio
async def test_discount_reservation_is_released_exactly_once(tmp_path) -> None:
    database = Database(f"sqlite+aiosqlite:///{tmp_path / 'bot.db'}")
    await database.initialize()

    try:
        async with database.session() as session:
            user = User(
                telegram_id=10,
                referral_code="discount-user",
                created_at=utc_now(),
                updated_at=utc_now(),
            )
            session.add(user)
            await session.flush()
            discount = UserDiscount(
                user_id=user.id,
                discount_bps=1500,
                scope="all",
                starts_at_utc=utc_now(),
                max_uses=1,
                used_count=0,
                status="active",
                created_by_admin_telegram_id=999,
                created_at_utc=utc_now(),
            )
            session.add(discount)
            await session.flush()
            order = Order(
                user_id=user.id,
                status="pending",
                period_count=1,
                duration_days=30,
                selected_max_devices=1,
                amount_minor_units=85,
                currency="XTR",
                provider="telegram_stars",
                pricing_version="test",
                order_kind="purchase",
                personal_discount_id=discount.id,
                personal_discount_bps=1500,
                personal_discount_amount_minor_units=15,
                invoice_payload="test-discount-order",
                created_at=utc_now(),
            )
            session.add(order)
            await session.flush()

            await reserve_discount_for_order(session, order)
            assert discount.used_count == 1
            await release_discount_for_order(session, order.id)
            await release_discount_for_order(session, order.id)
            assert discount.used_count == 0

            redemption = (
                await session.execute(
                    select(DiscountRedemption).where(DiscountRedemption.order_id == order.id)
                )
            ).scalar_one()
            assert redemption.status == "released"
    finally:
        await database.dispose()


@pytest.mark.asyncio
async def test_paid_remaining_time_uses_paid_segments(tmp_path) -> None:
    database = Database(f"sqlite+aiosqlite:///{tmp_path / 'bot.db'}")
    await database.initialize()

    try:
        now = utc_now()

        async with database.session() as session:
            user = User(
                telegram_id=20,
                referral_code="segment-user",
                created_at=now,
                updated_at=now,
            )
            session.add(user)
            await session.flush()
            subscription = Subscription(
                user_id=user.id,
                public_guid="00000000-0000-0000-0000-000000000020",
                signed_url="",
                max_devices=1,
                status="active",
                starts_at=now,
                expires_at=now + timedelta(days=40),
                created_at=now,
                updated_at_utc=now,
            )
            session.add(subscription)
            await session.flush()

            await record_entitlement_segment_once(
                session,
                subscription_id=subscription.id,
                source_kind="paid_order",
                starts_at_utc=now,
                ends_at_utc=now + timedelta(days=30),
                idempotency_key="paid-segment-test",
            )
            session.add(
                CommercialEntitlementSegment(
                    subscription_id=subscription.id,
                    source_kind="referral_reward",
                    starts_at_utc=now + timedelta(days=30),
                    ends_at_utc=now + timedelta(days=40),
                    idempotency_key="referral-segment-test",
                    status="applied",
                    created_at_utc=now,
                )
            )
            await session.flush()

            remaining = await remaining_paid_seconds(session, subscription, now)

        assert remaining == 30 * 86400
    finally:
        await database.dispose()


@pytest.mark.asyncio
async def test_paid_history_is_detected_from_applied_paid_order(tmp_path) -> None:
    from vpn_access_bot.models import OrderApplication
    from vpn_access_bot.product_completion import user_has_paid_history

    database = Database(f"sqlite+aiosqlite:///{tmp_path / 'bot.db'}")
    await database.initialize()

    try:
        async with database.session() as session:
            now = utc_now()
            user = User(
                telegram_id=20,
                referral_code="paid-user",
                created_at=now,
                updated_at=now,
            )
            session.add(user)
            await session.flush()

            subscription = Subscription(
                user_id=user.id,
                public_guid="00000000-0000-0000-0000-000000000020",
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

            order = Order(
                user_id=user.id,
                status="paid",
                period_count=1,
                duration_days=30,
                selected_max_devices=1,
                amount_minor_units=60,
                currency="XTR",
                provider="telegram_stars",
                pricing_version="test",
                order_kind="purchase",
                invoice_payload="paid-history-order",
                public_order_id="paid-history-public-order",
                created_at=now,
                paid_at=now,
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
            await session.flush()

            assert await user_has_paid_history(session, user.id) is True
    finally:
        await database.dispose()


@pytest.mark.asyncio
async def test_complimentary_order_does_not_count_as_paid_history(tmp_path) -> None:
    from vpn_access_bot.product_completion import user_has_paid_history

    database = Database(f"sqlite+aiosqlite:///{tmp_path / 'bot.db'}")
    await database.initialize()

    try:
        async with database.session() as session:
            now = utc_now()
            user = User(
                telegram_id=29,
                referral_code="complimentary-order-user",
                created_at=now,
                updated_at=now,
            )
            session.add(user)
            await session.flush()
            session.add(
                Order(
                    user_id=user.id,
                    status="paid",
                    period_count=1,
                    duration_days=30,
                    selected_max_devices=1,
                    amount_minor_units=0,
                    final_amount_minor_units=0,
                    price_before_personal_discount=100,
                    personal_discount_id=1,
                    personal_discount_bps=10_000,
                    personal_discount_amount_minor_units=100,
                    currency="XTR",
                    provider="telegram_stars",
                    pricing_version="test",
                    order_kind="purchase",
                    invoice_payload="complimentary-history-order",
                    public_order_id="complimentary-history-public-order",
                    referral_eligible=False,
                    created_at=now,
                    completed_at_utc=now,
                )
            )
            await session.flush()

            assert await user_has_paid_history(session, user.id) is False
    finally:
        await database.dispose()


@pytest.mark.asyncio
async def test_test_order_does_not_count_as_paid_history(tmp_path) -> None:
    from vpn_access_bot.product_completion import user_has_paid_history

    database = Database(f"sqlite+aiosqlite:///{tmp_path / 'bot.db'}")
    await database.initialize()

    try:
        async with database.session() as session:
            now = utc_now()
            user = User(
                telegram_id=30,
                referral_code="test-order-user",
                created_at=now,
                updated_at=now,
            )
            session.add(user)
            await session.flush()
            session.add(
                Order(
                    user_id=user.id,
                    status="paid",
                    period_count=1,
                    duration_days=30,
                    selected_max_devices=1,
                    amount_minor_units=1,
                    currency="XTR",
                    provider="telegram_stars",
                    pricing_version="test:admin-test",
                    order_kind="purchase",
                    invoice_payload="admin-test-order",
                    public_order_id="admin-test-public-order",
                    is_test_order=True,
                    referral_eligible=False,
                    created_at=now,
                    paid_at=now,
                    completed_at_utc=now,
                )
            )
            await session.flush()

            assert await user_has_paid_history(session, user.id) is False
    finally:
        await database.dispose()


@pytest.mark.asyncio
async def test_notification_claim_is_single_and_retryable_after_failure(tmp_path) -> None:
    from vpn_access_bot.repositories import NotificationDeliveryRepository

    database = Database(f"sqlite+aiosqlite:///{tmp_path / 'bot.db'}")
    await database.initialize()

    try:
        async with database.session() as session:
            now = utc_now()
            user = User(
                telegram_id=31,
                referral_code="notification-user",
                created_at=now,
                updated_at=now,
            )
            session.add(user)
            await session.flush()
            subscription = Subscription(
                user_id=user.id,
                public_guid="00000000-0000-0000-0000-000000000031",
                signed_url="",
                max_devices=1,
                status="active",
                starts_at=now,
                expires_at=now + timedelta(days=1),
                created_at=now,
                updated_at_utc=now,
            )
            session.add(subscription)
            await session.flush()
            repository = NotificationDeliveryRepository(session)
            first = await repository.claim(subscription.id, "subscription_1d", "key")
            assert first is not None
            assert await repository.claim(subscription.id, "subscription_1d", "key") is None
            await repository.mark_failed(first.id, "temporary")

        async with database.session() as session:
            repository = NotificationDeliveryRepository(session)
            retry = await repository.claim(subscription.id, "subscription_1d", "key")
            assert retry is not None
            await repository.mark_delivered(retry.id)

        async with database.session() as session:
            delivery = (await session.execute(select(NotificationDelivery))).scalar_one()
            assert delivery.status == "provider_accepted"
            assert delivery.provider_accepted_at_utc is not None
            assert delivery.delivered_at_utc is None
            assert delivery.attempt_count == 2
    finally:
        await database.dispose()


@pytest.mark.asyncio
async def test_stale_notification_claim_can_be_recovered_after_restart(tmp_path) -> None:
    from vpn_access_bot.repositories import NotificationDeliveryRepository

    database = Database(f"sqlite+aiosqlite:///{tmp_path / 'bot.db'}")
    await database.initialize()

    try:
        async with database.session() as session:
            now = utc_now()
            user = User(
                telegram_id=32,
                referral_code="stale-notification-user",
                created_at=now,
                updated_at=now,
            )
            session.add(user)
            await session.flush()
            subscription = Subscription(
                user_id=user.id,
                public_guid="00000000-0000-0000-0000-000000000032",
                signed_url="",
                max_devices=1,
                status="active",
                starts_at=now,
                expires_at=now + timedelta(days=1),
                created_at=now,
                updated_at_utc=now,
            )
            session.add(subscription)
            await session.flush()
            repository = NotificationDeliveryRepository(session)
            first = await repository.claim(subscription.id, "subscription_1d", "stale-key")
            assert first is not None
            first.claimed_at_utc = now - timedelta(minutes=16)

        async with database.session() as session:
            repository = NotificationDeliveryRepository(session)
            retry = await repository.claim(subscription.id, "subscription_1d", "stale-key")
            assert retry is not None
            assert retry.attempt_count == 2
    finally:
        await database.dispose()


@pytest.mark.asyncio
async def test_background_onboarding_completion_closes_session_and_notifies_once(tmp_path) -> None:
    from vpn_access_bot.mediator_client import DeviceTokenListItem
    from vpn_access_bot.models import OnboardingSession, ProductEvent
    from vpn_access_bot.product_completion import process_onboarding_completions_once

    class FakeMediator:
        async def list_device_tokens(self, public_guid: str):
            assert public_guid == "00000000-0000-0000-0000-000000000033"
            return [
                DeviceTokenListItem(
                    public_id="device-public-id",
                    display_name="Phone",
                    state="active",
                    pending_expires_at_utc=None,
                    activated_at_utc=utc_now().isoformat(),
                    first_fetched_at_utc=utc_now().isoformat(),
                    last_used_at_utc=None,
                    revoked_at_utc=None,
                    revocation_reason=None,
                    device_type="phone",
                    platform="android",
                )
            ]

    class FakeBot:
        def __init__(self) -> None:
            self.messages: list[tuple[int, str]] = []

        async def send_message(self, telegram_id: int, text: str, **kwargs) -> None:
            _ = kwargs
            self.messages.append((telegram_id, text))

    database = Database(f"sqlite+aiosqlite:///{tmp_path / 'bot.db'}")
    await database.initialize()
    bot = FakeBot()

    try:
        async with database.session() as session:
            now = utc_now()
            user = User(
                telegram_id=33,
                referral_code="completion-user",
                created_at=now,
                updated_at=now,
            )
            session.add(user)
            await session.flush()
            subscription = Subscription(
                user_id=user.id,
                public_guid="00000000-0000-0000-0000-000000000033",
                signed_url="",
                max_devices=1,
                status="active",
                starts_at=now,
                expires_at=now + timedelta(days=1),
                created_at=now,
                updated_at_utc=now,
            )
            session.add(subscription)
            await session.flush()
            session.add(
                OnboardingSession(
                    user_id=user.id,
                    subscription_id=subscription.id,
                    platform="android",
                    current_step="waiting_first_fetch",
                    status="waiting_first_fetch",
                    handoff_claim_id=None,
                    device_public_id="device-public-id",
                    created_at_utc=now,
                    updated_at_utc=now,
                )
            )

        first = await process_onboarding_completions_once(
            database.session,
            FakeMediator(),
            bot,
        )
        second = await process_onboarding_completions_once(
            database.session,
            FakeMediator(),
            bot,
        )

        async with database.session() as session:
            onboarding = (await session.execute(select(OnboardingSession))).scalar_one()
            delivery = (
                await session.execute(
                    select(NotificationDelivery).where(
                        NotificationDelivery.notification_kind == "onboarding_completed"
                    )
                )
            ).scalar_one()
            events = set((await session.execute(select(ProductEvent.event_name))).scalars().all())

        assert first == 1
        assert second == 0
        assert onboarding.status == "completed"
        assert onboarding.device_public_id == "device-public-id"
        assert delivery.status == "provider_accepted"
        assert delivery.provider_accepted_at_utc is not None
        assert delivery.delivered_at_utc is None
        assert len(bot.messages) == 1
        assert "Подписка добавлена в Happ" in bot.messages[0][1]
        assert "VPN подключён" not in bot.messages[0][1]
        assert {
            "credential_first_fetched",
            "subscription_observed_by_client",
            "onboarding_completed",
        } <= events
        assert "device_activated" not in events
        assert "handoff_redeemed" not in events
    finally:
        await database.dispose()


@pytest.mark.asyncio
async def test_background_completion_supports_legacy_onboarding_identifier(tmp_path) -> None:
    from vpn_access_bot.mediator_client import DeviceTokenListItem
    from vpn_access_bot.models import OnboardingSession, ProductEvent
    from vpn_access_bot.product_completion import process_onboarding_completions_once

    class FakeMediator:
        async def list_device_tokens(self, public_guid: str):
            assert public_guid == "00000000-0000-0000-0000-000000000034"
            return [
                DeviceTokenListItem(
                    public_id="legacy-device-id",
                    display_name="Legacy phone",
                    state="active",
                    pending_expires_at_utc=None,
                    activated_at_utc=utc_now().isoformat(),
                    first_fetched_at_utc=utc_now().isoformat(),
                    last_used_at_utc=None,
                    revoked_at_utc=None,
                    revocation_reason=None,
                    device_type="phone",
                    platform="android",
                )
            ]

    class FakeBot:
        def __init__(self) -> None:
            self.messages: list[tuple[int, str]] = []

        async def send_message(self, telegram_id: int, text: str, **kwargs) -> None:
            _ = kwargs
            self.messages.append((telegram_id, text))

    database = Database(f"sqlite+aiosqlite:///{tmp_path / 'legacy-onboarding.db'}")
    await database.initialize()
    bot = FakeBot()

    try:
        async with database.session() as session:
            now = utc_now()
            user = User(
                telegram_id=34,
                referral_code="legacy-completion-user",
                created_at=now,
                updated_at=now,
            )
            session.add(user)
            await session.flush()
            subscription = Subscription(
                user_id=user.id,
                public_guid="00000000-0000-0000-0000-000000000034",
                signed_url="",
                max_devices=1,
                status="active",
                starts_at=now,
                expires_at=now + timedelta(days=1),
                created_at=now,
                updated_at_utc=now,
            )
            session.add(subscription)
            await session.flush()
            session.add(
                OnboardingSession(
                    user_id=user.id,
                    subscription_id=subscription.id,
                    platform="android",
                    current_step="waiting_activation",
                    status="waiting_activation",
                    handoff_claim_id="legacy-device-id",
                    device_public_id=None,
                    created_at_utc=now,
                    updated_at_utc=now,
                )
            )

        completed = await process_onboarding_completions_once(
            database.session,
            FakeMediator(),
            bot,
        )

        async with database.session() as session:
            onboarding = (await session.execute(select(OnboardingSession))).scalar_one()
            events = set((await session.execute(select(ProductEvent.event_name))).scalars().all())

        assert completed == 1
        assert onboarding.status == "completed"
        assert onboarding.device_public_id == "legacy-device-id"
        assert "legacy_credential_first_fetched" in events
        assert "credential_first_fetched" not in events
        assert "device_activated" not in events
        assert len(bot.messages) == 1
    finally:
        await database.dispose()


@pytest.mark.asyncio
async def test_operational_alert_for_failed_paid_order_is_sent_once(tmp_path) -> None:
    from vpn_access_bot.config import Settings
    from vpn_access_bot.mediator_client import MediatorReadiness
    from vpn_access_bot.models import ProductEvent
    from vpn_access_bot.product_completion import send_operational_alerts_once

    class FakeMediator:
        async def get_readiness(self) -> MediatorReadiness:
            return MediatorReadiness(
                status="ready",
                catalog_state="fresh",
                server_count=2,
                migrations_applied=8,
                migrations_current=True,
                device_issuance_version=2,
                unified_subscription_feed_enabled=True,
                shared_subscription_links_only=True,
            )

    class FakeBot:
        def __init__(self) -> None:
            self.messages: list[tuple[int, str]] = []

        async def send_message(self, telegram_id: int, text: str, **kwargs) -> None:
            _ = kwargs
            self.messages.append((telegram_id, text))

    database = Database(f"sqlite+aiosqlite:///{tmp_path / 'bot.db'}")
    await database.initialize()
    bot = FakeBot()
    settings = Settings(
        TELEGRAM_BOT_TOKEN="test-token",
        MEDIATOR_ADMIN_TOKEN="test-admin-token",
        ADMIN_TELEGRAM_IDS="9001",
    )

    try:
        async with database.session() as session:
            now = utc_now()
            user = User(
                telegram_id=9000,
                referral_code="failed-order-user",
                created_at=now,
                updated_at=now,
            )
            session.add(user)
            await session.flush()
            session.add(
                Order(
                    user_id=user.id,
                    public_order_id="failed-paid-order",
                    status="activation_failed",
                    period_count=1,
                    duration_days=30,
                    selected_max_devices=1,
                    amount_minor_units=60,
                    currency="XTR",
                    provider="telegram_stars",
                    provider_payment_id="provider-charge-failed-order",
                    pricing_version="test",
                    order_kind="purchase",
                    invoice_payload="failed-paid-order-payload",
                    created_at=now,
                    paid_at=now,
                    last_activation_error_code="mediator_unavailable",
                )
            )

        first = await send_operational_alerts_once(
            database.session,
            FakeMediator(),
            bot,
            settings,
        )
        second = await send_operational_alerts_once(
            database.session,
            FakeMediator(),
            bot,
            settings,
        )

        async with database.session() as session:
            alert_events = list(
                (
                    await session.execute(
                        select(ProductEvent).where(
                            ProductEvent.event_name == "operational_alert_sent"
                        )
                    )
                )
                .scalars()
                .all()
            )

        assert first == 1
        assert second == 0
        assert len(bot.messages) == 1
        assert "failed-paid-order" in bot.messages[0][1]
        assert len(alert_events) == 1
    finally:
        await database.dispose()


@pytest.mark.asyncio
async def test_renewal_notification_contains_direct_renew_action(tmp_path) -> None:
    from vpn_access_bot.config import Settings
    from vpn_access_bot.product_completion import send_notifications_once

    class FakeBot:
        def __init__(self) -> None:
            self.messages: list[tuple[int, str, dict[str, object]]] = []

        async def send_message(self, telegram_id: int, text: str, **kwargs) -> None:
            self.messages.append((telegram_id, text, kwargs))

    database = Database(f"sqlite+aiosqlite:///{tmp_path / 'notifications.db'}")
    await database.initialize()
    settings = Settings(
        TELEGRAM_BOT_TOKEN="test-token",
        MEDIATOR_ADMIN_TOKEN="test-admin-token",
    )
    bot = FakeBot()

    try:
        async with database.session() as session:
            now = utc_now()
            user = User(
                telegram_id=404,
                referral_code="renew-notice-user",
                created_at=now,
                updated_at=now,
            )
            session.add(user)
            await session.flush()
            session.add(
                Subscription(
                    user_id=user.id,
                    public_guid="00000000-0000-0000-0000-000000000404",
                    signed_url="",
                    max_devices=3,
                    status="active",
                    starts_at=now,
                    expires_at=now + timedelta(days=2, hours=12),
                    created_at=now,
                    updated_at_utc=now,
                )
            )

        sent = await send_notifications_once(database.session, bot, settings)

        assert sent == 1
        assert len(bot.messages) == 1
        _, text, kwargs = bot.messages[0]
        assert "Доступ закончится через 3 дня" in text
        keyboard = kwargs["reply_markup"]
        button = keyboard.inline_keyboard[0][0]
        assert button.text == "Продлить доступ"
        assert button.callback_data == "buy:renew"
    finally:
        await database.dispose()


def test_referral_copy_explains_reward_with_examples() -> None:
    from vpn_access_bot.handlers.product_completion import _referral_text

    text = _referral_text("https://t.me/example?start=ref_code", 2, 12 * 86400)

    assert "30 дней — вам добавится 6 дней" in text
    assert "6 месяцев — вам добавится примерно 36 дней" in text
    assert "Начислено дней: <b>12</b>" in text


@pytest.mark.asyncio
async def test_released_discount_is_restored_exactly_once_for_confirmed_payment(
    tmp_path,
) -> None:
    from vpn_access_bot.repositories import DiscountRedemptionRepository

    database = Database(f"sqlite+aiosqlite:///{tmp_path / 'discount-restore.db'}")
    await database.initialize()

    try:
        async with database.session() as session:
            user = User(
                telegram_id=77,
                referral_code="discount-restore-user",
                created_at=utc_now(),
                updated_at=utc_now(),
            )
            session.add(user)
            await session.flush()
            discount = UserDiscount(
                user_id=user.id,
                discount_bps=1000,
                scope="all",
                starts_at_utc=utc_now(),
                max_uses=1,
                used_count=0,
                status="active",
                created_by_admin_telegram_id=999,
                created_at_utc=utc_now(),
            )
            session.add(discount)
            await session.flush()
            order = Order(
                user_id=user.id,
                status="expired",
                period_count=1,
                duration_days=30,
                selected_max_devices=1,
                amount_minor_units=90,
                currency="XTR",
                provider="telegram_stars",
                pricing_version="test",
                order_kind="purchase",
                personal_discount_id=discount.id,
                personal_discount_bps=1000,
                personal_discount_amount_minor_units=10,
                invoice_payload="discount-restore-order",
                created_at=utc_now(),
            )
            session.add(order)
            await session.flush()
            session.add(
                DiscountRedemption(
                    discount_id=discount.id,
                    order_id=order.id,
                    status="released",
                    reserved_at_utc=utc_now() - timedelta(minutes=1),
                    released_at_utc=utc_now(),
                    discount_amount_minor_units=10,
                )
            )
            await session.flush()

            repository = DiscountRedemptionRepository(session)
            await repository.restore_for_paid_order(order.id)
            await repository.restore_for_paid_order(order.id)

        async with database.session() as session:
            stored_discount = await session.get(UserDiscount, discount.id)
            redemption = (
                await session.execute(
                    select(DiscountRedemption).where(DiscountRedemption.order_id == order.id)
                )
            ).scalar_one()
            assert stored_discount is not None
            assert stored_discount.used_count == 1
            assert redemption.status == "reserved"
            assert redemption.released_at_utc is None
    finally:
        await database.dispose()
