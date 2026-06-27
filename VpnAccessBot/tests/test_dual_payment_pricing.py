from __future__ import annotations

from vpn_access_bot.commerce import PricingService
from vpn_access_bot.config import Settings
from vpn_access_bot.constants import (
    PAYMENT_MODE_TELEGRAM_STARS,
    PAYMENT_MODE_YOOKASSA_SBP,
)
from vpn_access_bot.models import PurchaseQuote, utc_now


def test_same_product_has_independent_stars_and_ruble_prices() -> None:
    settings = Settings(
        TELEGRAM_BOT_TOKEN="test-token",
        MEDIATOR_ADMIN_TOKEN="test-admin-token",
        PRICING_BASE_DEVICE_MONTH_STARS=60,
        PRICING_BASE_DEVICE_MONTH_RUB_KOPECKS=19900,
    )
    pricing = PricingService(settings)

    stars = pricing.calculate_operation(
        operation_kind="purchase",
        period_count=1,
        requested_max_devices=1,
        payment_provider=PAYMENT_MODE_TELEGRAM_STARS,
    )
    rubles = pricing.calculate_operation(
        operation_kind="purchase",
        period_count=1,
        requested_max_devices=1,
        payment_provider=PAYMENT_MODE_YOOKASSA_SBP,
    )

    assert (stars.amount_minor_units, stars.currency) == (60, "XTR")
    assert (rubles.amount_minor_units, rubles.currency) == (19900, "RUB")
    assert stars.pricing_version != rubles.pricing_version


def test_upgrade_quote_reuses_exact_paid_time_snapshot_for_ruble_price() -> None:
    settings = Settings(
        TELEGRAM_BOT_TOKEN="test-token",
        MEDIATOR_ADMIN_TOKEN="test-admin-token",
        PRICING_BASE_DEVICE_MONTH_STARS=60,
        PRICING_BASE_DEVICE_MONTH_RUB_KOPECKS=19900,
    )
    quote = PurchaseQuote(
        period_count=0,
        duration_days=0,
        max_devices=2,
        requested_max_devices=2,
        base_max_devices=1,
        amount_minor_units=30,
        currency="XTR",
        pricing_version="snapshot",
        order_kind="upgrade_devices",
        remaining_paid_seconds_at_quote=15 * 24 * 60 * 60,
        personal_discount_bps=0,
        expires_at_utc=utc_now(),
    )

    offer = PricingService(settings).calculate_quote_offer(quote, PAYMENT_MODE_YOOKASSA_SBP)

    assert offer.amount_minor_units == 9950
