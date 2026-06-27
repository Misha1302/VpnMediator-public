from __future__ import annotations

from datetime import timedelta

import pytest

from vpn_access_bot.constants import (
    ENTITLEMENT_STATUS_ACTIVE,
    ENTITLEMENT_STATUS_DISABLED,
    ENTITLEMENT_STATUS_EXPIRED,
    SUBSCRIPTION_STATUS_ACTIVE,
    SUBSCRIPTION_STATUS_DISABLED,
    SUBSCRIPTION_STATUS_EXPIRED,
)
from vpn_access_bot.entitlement_lifecycle import (
    is_expiration_transition_pending,
    lifecycle_matches_authoritative_entitlement,
    project_reconciled_lifecycle,
)
from vpn_access_bot.models import utc_now


def test_active_entitlement_projects_active_business_lifecycle() -> None:
    now = utc_now()

    projection = project_reconciled_lifecycle(
        current_subscription_status=SUBSCRIPTION_STATUS_EXPIRED,
        entitlement_status=ENTITLEMENT_STATUS_ACTIVE,
        valid_until_utc=now + timedelta(days=1),
        repair_mode="adopt_remote",
        now_utc=now,
    )

    assert projection.subscription_status == SUBSCRIPTION_STATUS_ACTIVE
    assert projection.disabled_at_utc is None


def test_active_entitlement_with_past_validity_is_rejected() -> None:
    now = utc_now()

    with pytest.raises(ValueError, match="active_entitlement_already_expired"):
        project_reconciled_lifecycle(
            current_subscription_status=SUBSCRIPTION_STATUS_ACTIVE,
            entitlement_status=ENTITLEMENT_STATUS_ACTIVE,
            valid_until_utc=now - timedelta(seconds=1),
            repair_mode="adopt_remote",
            now_utc=now,
        )


def test_expired_entitlement_projects_expired_business_lifecycle() -> None:
    now = utc_now()

    projection = project_reconciled_lifecycle(
        current_subscription_status=SUBSCRIPTION_STATUS_ACTIVE,
        entitlement_status=ENTITLEMENT_STATUS_EXPIRED,
        valid_until_utc=now - timedelta(seconds=1),
        repair_mode="adopt_remote",
        now_utc=now,
    )

    assert projection.subscription_status == SUBSCRIPTION_STATUS_EXPIRED
    assert projection.disabled_at_utc is None


def test_expired_entitlement_with_future_validity_is_rejected() -> None:
    now = utc_now()

    with pytest.raises(ValueError, match="expired_entitlement_has_future_validity"):
        project_reconciled_lifecycle(
            current_subscription_status=SUBSCRIPTION_STATUS_EXPIRED,
            entitlement_status=ENTITLEMENT_STATUS_EXPIRED,
            valid_until_utc=now + timedelta(seconds=1),
            repair_mode="adopt_remote",
            now_utc=now,
        )


def test_unknown_disabled_entitlement_is_not_projected_automatically() -> None:
    now = utc_now()

    with pytest.raises(ValueError, match="ambiguous_disabled_entitlement_origin"):
        project_reconciled_lifecycle(
            current_subscription_status=SUBSCRIPTION_STATUS_EXPIRED,
            entitlement_status=ENTITLEMENT_STATUS_DISABLED,
            valid_until_utc=now - timedelta(days=1),
            repair_mode="adopt_remote",
            now_utc=now,
        )


def test_explicit_legacy_expiration_keeps_subscription_resumable() -> None:
    now = utc_now()

    projection = project_reconciled_lifecycle(
        current_subscription_status=SUBSCRIPTION_STATUS_EXPIRED,
        entitlement_status=ENTITLEMENT_STATUS_DISABLED,
        valid_until_utc=now - timedelta(days=1),
        repair_mode="adopt_expired",
        now_utc=now,
    )

    assert projection.subscription_status == SUBSCRIPTION_STATUS_EXPIRED
    assert projection.disabled_at_utc is None
    assert projection.reason_code == "legacy_expiration"


@pytest.mark.parametrize(
    ("subscription_status", "entitlement_status", "validity_delta", "expected"),
    [
        (
            SUBSCRIPTION_STATUS_ACTIVE,
            ENTITLEMENT_STATUS_ACTIVE,
            timedelta(days=1),
            True,
        ),
        (
            SUBSCRIPTION_STATUS_ACTIVE,
            ENTITLEMENT_STATUS_ACTIVE,
            timedelta(seconds=-1),
            False,
        ),
        (
            SUBSCRIPTION_STATUS_EXPIRED,
            ENTITLEMENT_STATUS_EXPIRED,
            timedelta(seconds=-1),
            True,
        ),
        (
            SUBSCRIPTION_STATUS_DISABLED,
            ENTITLEMENT_STATUS_DISABLED,
            timedelta(days=1),
            True,
        ),
        (
            SUBSCRIPTION_STATUS_EXPIRED,
            ENTITLEMENT_STATUS_DISABLED,
            timedelta(seconds=-1),
            True,
        ),
    ],
)
def test_lifecycle_consistency_matrix(
    subscription_status: str,
    entitlement_status: str,
    validity_delta: timedelta,
    expected: bool,
) -> None:
    now = utc_now()

    assert (
        lifecycle_matches_authoritative_entitlement(
            subscription_status=subscription_status,
            entitlement_status=entitlement_status,
            valid_until_utc=now + validity_delta,
            now_utc=now,
        )
        is expected
    )


def test_matching_expired_active_snapshot_is_known_expiration_transition() -> None:
    now = utc_now()
    expired_at = now - timedelta(seconds=1)

    assert is_expiration_transition_pending(
        subscription_status=SUBSCRIPTION_STATUS_ACTIVE,
        subscription_expires_at_utc=expired_at,
        local_status=ENTITLEMENT_STATUS_ACTIVE,
        local_valid_until_utc=expired_at,
        remote_status=ENTITLEMENT_STATUS_ACTIVE,
        remote_valid_until_utc=expired_at,
        now_utc=now,
    )


def test_future_active_snapshot_is_not_expiration_transition() -> None:
    now = utc_now()
    future = now + timedelta(minutes=1)

    assert not is_expiration_transition_pending(
        subscription_status=SUBSCRIPTION_STATUS_ACTIVE,
        subscription_expires_at_utc=future,
        local_status=ENTITLEMENT_STATUS_ACTIVE,
        local_valid_until_utc=future,
        remote_status=ENTITLEMENT_STATUS_ACTIVE,
        remote_valid_until_utc=future,
        now_utc=now,
    )


def test_explicit_disabled_adoption_projects_disabled_business_lifecycle() -> None:
    now = utc_now()

    projection = project_reconciled_lifecycle(
        current_subscription_status=SUBSCRIPTION_STATUS_ACTIVE,
        entitlement_status=ENTITLEMENT_STATUS_DISABLED,
        valid_until_utc=now + timedelta(days=1),
        repair_mode="adopt_disabled",
        now_utc=now,
    )

    assert projection.subscription_status == SUBSCRIPTION_STATUS_DISABLED
    assert projection.disabled_at_utc == now
    assert projection.reason_code == "explicit_disabled_adoption"
