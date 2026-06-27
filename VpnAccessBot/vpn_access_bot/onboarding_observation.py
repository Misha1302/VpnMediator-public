from __future__ import annotations

from collections.abc import Iterable
from datetime import UTC, datetime

from vpn_access_bot.mediator_client import DeviceTokenListItem


def find_first_fetched_device(
    devices: Iterable[DeviceTokenListItem],
    expected_public_id: str | None,
    feed_issued_at_utc: datetime | None,
) -> DeviceTokenListItem | None:
    """Find the device that proves the current onboarding link was observed by Happ."""
    if expected_public_id is not None:
        return next(
            (
                device
                for device in devices
                if device.public_id == expected_public_id
                and device.first_fetched_at_utc is not None
            ),
            None,
        )

    issued_at = _to_aware_utc(feed_issued_at_utc) if feed_issued_at_utc is not None else None
    candidates: list[tuple[datetime, str, DeviceTokenListItem]] = []

    for device in devices:
        if device.access_channel != "unified_feed" or device.first_fetched_at_utc is None:
            continue

        first_fetched_at = _parse_mediator_datetime(device.first_fetched_at_utc)

        if first_fetched_at is None:
            continue
        if issued_at is not None and first_fetched_at < issued_at:
            continue

        candidates.append((first_fetched_at, device.public_id, device))

    if not candidates:
        return None

    return max(candidates, key=lambda candidate: (candidate[0], candidate[1]))[2]


def _parse_mediator_datetime(value: str | None) -> datetime | None:
    if not value:
        return None

    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None

    return _to_aware_utc(parsed)


def _to_aware_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)

    return value.astimezone(UTC)
