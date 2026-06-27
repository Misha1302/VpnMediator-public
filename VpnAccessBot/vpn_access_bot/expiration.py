from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from zoneinfo import ZoneInfo

LEGACY_EXPIRATION_POLICY_VERSION = "legacy-exact-duration-v1"


@dataclass(frozen=True)
class ExpirationSnapshot:
    base_expires_at_utc: datetime
    purchased_duration_days: int
    expiration_policy_version: str
    target_expires_at_utc: datetime


def calculate_expiration_snapshot(
    *,
    current_expires_at_utc: datetime | None,
    captured_now_utc: datetime,
    purchased_duration_days: int,
    order_kind: str,
    business_timezone: str,
    configured_policy_version: str,
    policy_effective_at_utc: datetime,
) -> ExpirationSnapshot:
    now = _aware_utc(captured_now_utc)
    effective_at = _aware_utc(policy_effective_at_utc)
    current = _aware_utc(current_expires_at_utc) if current_expires_at_utc else None
    base = current if current is not None and current > now else now

    if purchased_duration_days < 0:
        raise ValueError("Purchased duration must not be negative.")

    if order_kind == "upgrade_devices":
        return ExpirationSnapshot(
            base_expires_at_utc=base,
            purchased_duration_days=0,
            expiration_policy_version=configured_policy_version,
            target_expires_at_utc=base,
        )

    if purchased_duration_days <= 0:
        raise ValueError("Duration order must contain a positive purchased duration.")

    if now < effective_at:
        target = base + timedelta(days=purchased_duration_days)
        policy_version = LEGACY_EXPIRATION_POLICY_VERSION
    else:
        timezone = ZoneInfo(business_timezone)
        nominal = base + timedelta(days=purchased_duration_days)
        nominal_local = nominal.astimezone(timezone)
        exclusive_local_date = nominal_local.date() + timedelta(days=1)
        exclusive_local = datetime.combine(
            exclusive_local_date,
            time.min,
            tzinfo=timezone,
        )
        target = exclusive_local.astimezone(UTC)
        policy_version = configured_policy_version

    return ExpirationSnapshot(
        base_expires_at_utc=base,
        purchased_duration_days=purchased_duration_days,
        expiration_policy_version=policy_version,
        target_expires_at_utc=target,
    )


def access_through_date(expires_at_utc: datetime, business_timezone: str) -> date:
    expires = _aware_utc(expires_at_utc)
    local_exclusive_date = expires.astimezone(ZoneInfo(business_timezone)).date()
    return local_exclusive_date - timedelta(days=1)


def _aware_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)
