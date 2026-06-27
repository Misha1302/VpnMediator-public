from __future__ import annotations

from datetime import UTC, datetime

import pytest

from vpn_access_bot.expiration import (
    LEGACY_EXPIRATION_POLICY_VERSION,
    access_through_date,
    calculate_expiration_snapshot,
)
from vpn_access_bot.formatting import format_access_through_date_ru

POLICY = "calendar-day-bonus-v2"
EFFECTIVE = datetime(2026, 6, 7, tzinfo=UTC)


def calculate(
    *,
    current: datetime | None,
    now: datetime,
    duration_days: int,
    kind: str = "purchase",
    timezone: str = "Europe/Moscow",
):
    return calculate_expiration_snapshot(
        current_expires_at_utc=current,
        captured_now_utc=now,
        purchased_duration_days=duration_days,
        order_kind=kind,
        business_timezone=timezone,
        configured_policy_version=POLICY,
        policy_effective_at_utc=EFFECTIVE,
    )


def test_new_purchase_gets_exactly_one_bonus_calendar_day() -> None:
    now = datetime(2026, 6, 7, 10, 30, tzinfo=UTC)

    snapshot = calculate(current=None, now=now, duration_days=30)

    assert snapshot.target_expires_at_utc == datetime(2026, 7, 7, 21, 0, tzinfo=UTC)
    assert access_through_date(snapshot.target_expires_at_utc, "Europe/Moscow").isoformat() == (
        "2026-07-07"
    )


def test_nominal_midnight_still_receives_one_bonus_day() -> None:
    now = datetime(2026, 6, 7, 21, 0, tzinfo=UTC)  # 00:00 in Moscow

    snapshot = calculate(current=None, now=now, duration_days=1)

    assert snapshot.target_expires_at_utc == datetime(2026, 6, 9, 21, 0, tzinfo=UTC)
    assert access_through_date(snapshot.target_expires_at_utc, "Europe/Moscow").isoformat() == (
        "2026-06-09"
    )


def test_active_renewal_preserves_remaining_time_before_calendar_rounding() -> None:
    now = datetime(2026, 6, 7, 10, 0, tzinfo=UTC)
    current = datetime(2026, 6, 20, 18, 0, tzinfo=UTC)

    snapshot = calculate(current=current, now=now, duration_days=30, kind="extend")

    assert snapshot.base_expires_at_utc == current
    assert snapshot.target_expires_at_utc == datetime(2026, 7, 20, 21, 0, tzinfo=UTC)


def test_expired_resume_uses_captured_now() -> None:
    now = datetime(2026, 6, 7, 10, 0, tzinfo=UTC)
    expired = datetime(2026, 6, 1, 0, 0, tzinfo=UTC)

    snapshot = calculate(current=expired, now=now, duration_days=30, kind="resume")

    assert snapshot.base_expires_at_utc == now


def test_upgrade_only_does_not_change_expiration() -> None:
    now = datetime(2026, 6, 7, 10, 0, tzinfo=UTC)
    current = datetime(2026, 7, 1, 0, 0, tzinfo=UTC)

    snapshot = calculate(
        current=current,
        now=now,
        duration_days=0,
        kind="upgrade_devices",
    )

    assert snapshot.target_expires_at_utc == current
    assert snapshot.purchased_duration_days == 0


def test_pre_effective_orders_keep_legacy_exact_duration() -> None:
    now = datetime(2026, 6, 6, 10, 0, tzinfo=UTC)

    snapshot = calculate(current=None, now=now, duration_days=30)

    assert snapshot.expiration_policy_version == LEGACY_EXPIRATION_POLICY_VERSION
    assert snapshot.target_expires_at_utc == datetime(2026, 7, 6, 10, 0, tzinfo=UTC)


def test_dst_timezone_uses_calendar_day_not_fixed_24_hour_rounding() -> None:
    effective = datetime(2026, 1, 1, tzinfo=UTC)
    now = datetime(2026, 3, 28, 11, 0, tzinfo=UTC)

    snapshot = calculate_expiration_snapshot(
        current_expires_at_utc=None,
        captured_now_utc=now,
        purchased_duration_days=1,
        order_kind="purchase",
        business_timezone="Europe/Amsterdam",
        configured_policy_version=POLICY,
        policy_effective_at_utc=effective,
    )

    assert snapshot.target_expires_at_utc == datetime(2026, 3, 29, 22, 0, tzinfo=UTC)
    assert access_through_date(snapshot.target_expires_at_utc, "Europe/Amsterdam").isoformat() == (
        "2026-03-29"
    )


def test_user_facing_expiration_contains_date_only() -> None:
    formatted = format_access_through_date_ru(
        datetime(2026, 7, 7, 21, 0, tzinfo=UTC), "Europe/Moscow"
    )

    assert formatted == "7 июля 2026 года включительно"
    assert ":" not in formatted


def test_negative_duration_is_rejected() -> None:
    with pytest.raises(ValueError, match="must not be negative"):
        calculate(
            current=None,
            now=datetime(2026, 6, 7, tzinfo=UTC),
            duration_days=-1,
        )
