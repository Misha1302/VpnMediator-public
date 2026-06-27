from __future__ import annotations

from datetime import UTC, datetime, timedelta

from vpn_access_bot.mediator_client import DeviceTokenListItem
from vpn_access_bot.onboarding_observation import find_first_fetched_device


def _device(
    public_id: str,
    *,
    access_channel: str = "unified_feed",
    first_fetched_at_utc: str | None,
    last_used_at_utc: str | None,
) -> DeviceTokenListItem:
    return DeviceTokenListItem(
        public_id=public_id,
        display_name=public_id,
        state="active",
        pending_expires_at_utc=None,
        activated_at_utc=None,
        first_fetched_at_utc=first_fetched_at_utc,
        last_used_at_utc=last_used_at_utc,
        revoked_at_utc=None,
        revocation_reason=None,
        access_channel=access_channel,
    )


def test_unified_feed_accepts_naive_sqlite_cutoff_and_recent_fetch() -> None:
    issued_at = datetime(2026, 6, 13, 15, 0, 0)
    fetched_at = datetime(2026, 6, 13, 15, 0, 1, tzinfo=UTC)
    device = _device(
        "new-device",
        first_fetched_at_utc=fetched_at.isoformat(),
        last_used_at_utc=fetched_at.isoformat(),
    )

    assert find_first_fetched_device([device], None, issued_at) == device


def test_unified_feed_uses_first_fetch_time_as_the_observation_boundary() -> None:
    issued_at = datetime(2026, 6, 13, 15, 0, 0, tzinfo=UTC)
    fetched_at = issued_at + timedelta(seconds=1)
    device = _device(
        "new-device",
        first_fetched_at_utc=fetched_at.isoformat(),
        last_used_at_utc=None,
    )

    assert find_first_fetched_device([device], None, issued_at) == device


def test_unified_feed_ignores_old_first_fetch_even_after_a_recent_refresh() -> None:
    issued_at = datetime(2026, 6, 13, 15, 0, 0, tzinfo=UTC)
    old_device = _device(
        "old-device",
        first_fetched_at_utc=(issued_at - timedelta(minutes=1)).isoformat(),
        last_used_at_utc=(issued_at + timedelta(seconds=1)).isoformat(),
    )
    malformed_device = _device(
        "malformed-device",
        first_fetched_at_utc="not-a-date",
        last_used_at_utc="not-a-date",
    )

    assert find_first_fetched_device([old_device, malformed_device], None, issued_at) is None


def test_explicit_device_identifier_preserves_legacy_matching() -> None:
    issued_at = datetime(2026, 6, 13, 15, 0, 0, tzinfo=UTC)
    fetched_before_issue = issued_at - timedelta(days=1)
    matching = _device(
        "legacy-device",
        access_channel="device_link",
        first_fetched_at_utc=fetched_before_issue.isoformat(),
        last_used_at_utc=fetched_before_issue.isoformat(),
    )

    assert find_first_fetched_device([matching], "legacy-device", issued_at) == matching
