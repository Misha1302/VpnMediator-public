from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime

from vpn_access_bot.constants import (
    ENTITLEMENT_STATUS_ACTIVE,
    ENTITLEMENT_STATUS_DISABLED,
    ENTITLEMENT_STATUS_EXPIRED,
    SUBSCRIPTION_STATUS_ACTIVE,
    SUBSCRIPTION_STATUS_DISABLED,
    SUBSCRIPTION_STATUS_EXPIRED,
)


def _to_aware_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


@dataclass(frozen=True, slots=True)
class LifecycleProjection:
    subscription_status: str
    disabled_at_utc: datetime | None
    reason_code: str


def project_reconciled_lifecycle(
    *,
    current_subscription_status: str,
    entitlement_status: str,
    valid_until_utc: datetime,
    repair_mode: str,
    now_utc: datetime,
) -> LifecycleProjection:
    valid_until = _to_aware_utc(valid_until_utc)
    now = _to_aware_utc(now_utc)

    if entitlement_status == ENTITLEMENT_STATUS_ACTIVE:
        if valid_until <= now:
            raise ValueError("active_entitlement_already_expired")
        return LifecycleProjection(
            subscription_status=SUBSCRIPTION_STATUS_ACTIVE,
            disabled_at_utc=None,
            reason_code="active_entitlement",
        )

    if entitlement_status == ENTITLEMENT_STATUS_EXPIRED:
        if valid_until > now:
            raise ValueError("expired_entitlement_has_future_validity")
        return LifecycleProjection(
            subscription_status=SUBSCRIPTION_STATUS_EXPIRED,
            disabled_at_utc=None,
            reason_code="expired_entitlement",
        )

    if entitlement_status != ENTITLEMENT_STATUS_DISABLED:
        raise ValueError("unsupported_entitlement_status")

    if repair_mode == "adopt_expired":
        if current_subscription_status != SUBSCRIPTION_STATUS_EXPIRED:
            raise ValueError("legacy_expiration_requires_expired_subscription")
        if valid_until > now:
            raise ValueError("legacy_expiration_has_future_validity")
        return LifecycleProjection(
            subscription_status=SUBSCRIPTION_STATUS_EXPIRED,
            disabled_at_utc=None,
            reason_code="legacy_expiration",
        )

    if repair_mode == "adopt_disabled":
        return LifecycleProjection(
            subscription_status=SUBSCRIPTION_STATUS_DISABLED,
            disabled_at_utc=now,
            reason_code="explicit_disabled_adoption",
        )

    if repair_mode == "restore_local":
        if current_subscription_status == SUBSCRIPTION_STATUS_EXPIRED:
            if valid_until > now:
                raise ValueError("expired_subscription_has_future_validity")
            return LifecycleProjection(
                subscription_status=SUBSCRIPTION_STATUS_EXPIRED,
                disabled_at_utc=None,
                reason_code="restored_legacy_expiration",
            )
        if current_subscription_status == SUBSCRIPTION_STATUS_DISABLED:
            return LifecycleProjection(
                subscription_status=SUBSCRIPTION_STATUS_DISABLED,
                disabled_at_utc=now,
                reason_code="restored_disabled_entitlement",
            )

    raise ValueError("ambiguous_disabled_entitlement_origin")


def lifecycle_matches_authoritative_entitlement(
    *,
    subscription_status: str,
    entitlement_status: str,
    valid_until_utc: datetime,
    now_utc: datetime,
) -> bool:
    valid_until = _to_aware_utc(valid_until_utc)
    now = _to_aware_utc(now_utc)

    if entitlement_status == ENTITLEMENT_STATUS_ACTIVE:
        return subscription_status == SUBSCRIPTION_STATUS_ACTIVE and valid_until > now
    if entitlement_status == ENTITLEMENT_STATUS_EXPIRED:
        return subscription_status == SUBSCRIPTION_STATUS_EXPIRED and valid_until <= now
    if entitlement_status == ENTITLEMENT_STATUS_DISABLED:
        return subscription_status in {
            SUBSCRIPTION_STATUS_DISABLED,
            SUBSCRIPTION_STATUS_EXPIRED,
        } and (subscription_status != SUBSCRIPTION_STATUS_EXPIRED or valid_until <= now)
    return False


def is_expiration_transition_pending(
    *,
    subscription_status: str,
    subscription_expires_at_utc: datetime,
    local_status: str,
    local_valid_until_utc: datetime,
    remote_status: str,
    remote_valid_until_utc: datetime,
    now_utc: datetime,
) -> bool:
    subscription_expires_at = _to_aware_utc(subscription_expires_at_utc)
    local_valid_until = _to_aware_utc(local_valid_until_utc)
    remote_valid_until = _to_aware_utc(remote_valid_until_utc)
    now = _to_aware_utc(now_utc)

    return (
        subscription_status == SUBSCRIPTION_STATUS_ACTIVE
        and subscription_expires_at <= now
        and local_status == ENTITLEMENT_STATUS_ACTIVE
        and remote_status == ENTITLEMENT_STATUS_ACTIVE
        and subscription_expires_at == local_valid_until == remote_valid_until
    )


def is_legacy_expiration_drift(
    *,
    subscription_status: str,
    subscription_expires_at_utc: datetime,
    local_status: str,
    local_version: int,
    local_valid_until_utc: datetime,
    local_max_device_tokens: int,
    remote_status: str,
    remote_version: int,
    remote_valid_until_utc: datetime,
    remote_max_device_tokens: int,
    now_utc: datetime,
) -> bool:
    subscription_expires_at = _to_aware_utc(subscription_expires_at_utc)
    local_valid_until = _to_aware_utc(local_valid_until_utc)
    remote_valid_until = _to_aware_utc(remote_valid_until_utc)
    now = _to_aware_utc(now_utc)

    return (
        subscription_status == SUBSCRIPTION_STATUS_EXPIRED
        and subscription_expires_at <= now
        and local_status == ENTITLEMENT_STATUS_ACTIVE
        and remote_status == ENTITLEMENT_STATUS_DISABLED
        and remote_version == local_version + 1
        and subscription_expires_at == local_valid_until == remote_valid_until
        and local_max_device_tokens == remote_max_device_tokens
    )
