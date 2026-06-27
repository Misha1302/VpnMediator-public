from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from vpn_access_bot.commerce import (
    CabinetStateBuilder,
    CabinetSubscriptionState,
    OrderNoticeState,
    PricingService,
    RefundPolicy,
    UserErrorCode,
)
from vpn_access_bot.config import Settings
from vpn_access_bot.error_texts import user_error_text
from vpn_access_bot.formatting import days_ru, devices_ru, months_ru
from vpn_access_bot.models import Order, OrderApplication, Subscription, utc_now
from vpn_access_bot.trial import TrialEligibility, TrialEligibilityReason


def make_settings() -> Settings:
    return Settings(
        TELEGRAM_BOT_TOKEN="test-token",
        PAYMENT_MODE="telegram_stars",
        MEDIATOR_ADMIN_TOKEN="test-admin-token",
        PRICING_BASE_DEVICE_MONTH_STARS=100,
        PRICING_DURATION_DISCOUNTS="1:0,3:10,12:20",
    )


def test_pricing_uses_server_side_limits_and_rounding() -> None:
    price = PricingService(make_settings()).calculate(period_count=3, max_devices=2)

    assert price.duration_days == 90
    assert price.amount_minor_units == 540
    assert price.currency == "XTR"
    assert price.pricing_version.startswith("closed-beta-2026-06-v2:")
    assert len(price.pricing_version.rsplit(":", 1)[1]) == 12


def test_pricing_allows_exactly_one_hundred_percent_personal_discount() -> None:
    price = PricingService(make_settings()).calculate_operation(
        operation_kind="purchase",
        period_count=1,
        requested_max_devices=1,
        personal_discount_bps=10_000,
    )

    assert price.price_before_personal_discount == 100
    assert price.personal_discount_amount_minor_units == 100
    assert price.amount_minor_units == 0


def test_russian_pluralization() -> None:
    assert devices_ru(1) == "1 устройство"
    assert devices_ru(3) == "3 устройства"
    assert devices_ru(12) == "12 устройств"
    assert months_ru(2) == "2 месяца"
    assert days_ru(21) == "21 день"


def test_cabinet_state_for_active_and_missing_subscription() -> None:
    builder = CabinetStateBuilder()
    eligibility = TrialEligibility(True, False, TrialEligibilityReason.AVAILABLE)
    none_state = builder.build(None, None, eligibility)

    assert none_state.subscription_state == CabinetSubscriptionState.NONE
    assert none_state.primary_action == "purchase"

    subscription = Subscription(
        id=1,
        user_id=1,
        public_guid="00000000-0000-0000-0000-000000000001",
        signed_url="",
        max_devices=3,
        status="active",
        starts_at=utc_now(),
        expires_at=datetime.now(UTC) + timedelta(days=10),
        created_at=utc_now(),
        updated_at_utc=utc_now(),
    )
    active_state = builder.build(subscription, None, eligibility, active_device_tokens=1)

    assert active_state.subscription_state == CabinetSubscriptionState.ACTIVE
    assert active_state.primary_action == "add_device"
    assert active_state.active_device_tokens == 1


def test_active_subscription_and_pending_order_are_rendered_independently() -> None:
    builder = CabinetStateBuilder()
    eligibility = TrialEligibility(False, False, TrialEligibilityReason.ACTIVE_SUBSCRIPTION_EXISTS)
    subscription = Subscription(
        id=1,
        user_id=1,
        public_guid="00000000-0000-0000-0000-000000000001",
        signed_url="",
        max_devices=3,
        status="active",
        starts_at=utc_now(),
        expires_at=datetime.now(UTC) + timedelta(days=10),
        created_at=utc_now(),
        updated_at_utc=utc_now(),
    )
    order = Order(
        id=1,
        public_order_id="pending-renewal",
        user_id=1,
        status="pending",
        amount_minor_units=100,
        currency="XTR",
        provider="telegram_stars",
        invoice_payload="payload",
        order_kind="extend",
        created_at=utc_now(),
    )

    state = builder.build(subscription, order, eligibility, active_device_tokens=1)

    assert state.subscription_state == CabinetSubscriptionState.ACTIVE
    assert state.order_notice_state == OrderNoticeState.PAYMENT_PENDING
    assert state.primary_action == "continue_order"
    assert state.pending_order_public_id == "pending-renewal"


def test_refund_policy_refuses_after_successful_application() -> None:
    order = Order(
        id=1,
        public_order_id="order-public",
        user_id=1,
        status="paid",
        amount_minor_units=100,
        currency="XTR",
        provider="telegram_stars",
        provider_payment_id="charge",
        invoice_payload="payload",
        created_at=utc_now(),
    )
    application = OrderApplication(
        id=1,
        order_id=1,
        subscription_id=1,
        applied_at_utc=utc_now(),
        duration_days=30,
        selected_max_devices=1,
        resulting_valid_until_utc=utc_now() + timedelta(days=30),
        resulting_entitlement_version=1,
    )

    allowed, reason = RefundPolicy().automatic_refund_allowed(order, application)

    assert allowed is False
    assert "Автоматический возврат невозможен" in reason


def test_user_error_mapping_has_next_action() -> None:
    assert "Нажмите" in user_error_text(UserErrorCode.NO_SUBSCRIPTION)
    assert "Попробуйте" in user_error_text(UserErrorCode.SERVICE_UNAVAILABLE)


@pytest.mark.parametrize(
    ("period_count", "max_devices"),
    [
        (2, 1),
        (1, 13),
        (0, 1),
        (-1, 1),
    ],
)
def test_pricing_rejects_crafted_unsupported_options(
    period_count: int,
    max_devices: int,
) -> None:
    with pytest.raises(ValueError):
        PricingService(make_settings()).calculate(period_count, max_devices)
