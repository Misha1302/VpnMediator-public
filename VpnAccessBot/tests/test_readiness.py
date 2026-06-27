from __future__ import annotations

from pathlib import Path

import pytest

from vpn_access_bot.advertising_readiness import (
    CommerceOperationKind,
    CommercePolicyRepository,
)
from vpn_access_bot.config import Settings
from vpn_access_bot.db import Database
from vpn_access_bot.mediator_client import MediatorClientError, MediatorReadiness
from vpn_access_bot.readiness import CommerceReadinessService


class FakeReadinessClient:
    def __init__(self, readiness: MediatorReadiness | None = None, *, fail: bool = False) -> None:
        self.readiness = readiness
        self.fail = fail
        self.calls = 0

    async def get_readiness(self) -> MediatorReadiness:
        self.calls += 1
        if self.fail:
            raise MediatorClientError("unavailable", error_code="mediator_unavailable")
        assert self.readiness is not None
        return self.readiness


def readiness(
    *,
    status: str = "ready",
    catalog_state: str = "fresh",
    server_count: int = 2,
    migrations_current: bool = True,
    device_issuance_version: int = 2,
    unified_subscription_feed_enabled: bool = True,
    shared_subscription_links_only: bool = True,
) -> MediatorReadiness:
    return MediatorReadiness(
        status=status,
        catalog_state=catalog_state,
        server_count=server_count,
        migrations_applied=8,
        migrations_current=migrations_current,
        device_issuance_version=device_issuance_version,
        unified_subscription_feed_enabled=unified_subscription_feed_enabled,
        shared_subscription_links_only=shared_subscription_links_only,
    )


@pytest.mark.asyncio
async def test_new_purchase_requires_fresh_catalog() -> None:
    client = FakeReadinessClient(readiness(catalog_state="stale"))

    result = await CommerceReadinessService(client, cache_seconds=0).check()  # type: ignore[arg-type]

    assert result.can_sell is False
    assert result.reason_code == "fresh_catalog_required"


@pytest.mark.asyncio
@pytest.mark.parametrize("catalog_state", ["fresh", "stale"])
async def test_renewal_allows_safe_fresh_or_stale_catalog(catalog_state: str) -> None:
    client = FakeReadinessClient(readiness(catalog_state=catalog_state))

    result = await CommerceReadinessService(client, cache_seconds=0).check(
        operation_kind=CommerceOperationKind.RENEWAL
    )  # type: ignore[arg-type]

    assert result.can_sell is True
    assert result.reason_code == "ready"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("mediator_readiness", "expected_reason"),
    [
        (readiness(server_count=0), "catalog_empty"),
        (readiness(migrations_current=False), "migrations_pending"),
        (readiness(device_issuance_version=1), "device_issuance_v2_unavailable"),
        (
            readiness(unified_subscription_feed_enabled=False),
            "unified_subscription_feed_unavailable",
        ),
        (
            readiness(shared_subscription_links_only=False),
            "shared_subscription_links_only_unavailable",
        ),
        (readiness(status="not_ready"), "service_not_ready"),
        (readiness(catalog_state="unavailable"), "catalog_unavailable"),
    ],
)
async def test_readiness_blocks_unsafe_sale(
    mediator_readiness: MediatorReadiness,
    expected_reason: str,
) -> None:
    client = FakeReadinessClient(mediator_readiness)

    result = await CommerceReadinessService(client, cache_seconds=0).check()  # type: ignore[arg-type]

    assert result.can_sell is False
    assert result.reason_code == expected_reason


@pytest.mark.asyncio
async def test_readiness_wraps_mediator_failure() -> None:
    client = FakeReadinessClient(fail=True)

    result = await CommerceReadinessService(client, cache_seconds=0).check()  # type: ignore[arg-type]

    assert result.can_sell is False
    assert result.reason_code == "mediator_unavailable"


@pytest.mark.asyncio
async def test_emergency_policy_stop_bypasses_positive_readiness_cache(
    tmp_path: Path,
) -> None:
    database = Database(f"sqlite+aiosqlite:///{tmp_path / 'readiness-policy-cache.db'}")
    await database.initialize()
    client = FakeReadinessClient(readiness())
    settings = Settings(
        TELEGRAM_BOT_TOKEN="test-token",
        MEDIATOR_ADMIN_TOKEN="test-admin-token",
    )
    service = CommerceReadinessService(
        client,  # type: ignore[arg-type]
        cache_seconds=60,
        session_factory=database.session,
        settings=settings,
    )
    try:
        async with database.session() as session:
            await CommercePolicyRepository(session).set_switch(
                switch_name="new_purchases",
                enabled=True,
                admin_telegram_id=1,
                reason_code="canary_start",
            )
            await session.commit()

        first = await service.check()
        assert first.allowed is True
        assert client.calls == 1

        async with database.session() as session:
            await CommercePolicyRepository(session).set_switch(
                switch_name="new_purchases",
                enabled=False,
                admin_telegram_id=1,
                reason_code="emergency_stop",
            )
            await session.commit()

        stopped = await service.check()
        assert stopped.allowed is False
        assert stopped.reason_code == "policy_new_purchase_disabled"
        assert client.calls == 1
    finally:
        await database.dispose()


@pytest.mark.asyncio
async def test_recovery_operation_does_not_depend_on_mediator_readiness() -> None:
    client = FakeReadinessClient(fail=True)
    result = await CommerceReadinessService(client, cache_seconds=60).check(
        operation_kind=CommerceOperationKind.REFUND_COMPENSATION
    )  # type: ignore[arg-type]

    assert result.allowed is True
    assert result.reason_code == "recovery_only"
    assert client.calls == 0


@pytest.mark.asyncio
async def test_all_decisions_uses_one_mediator_snapshot() -> None:
    client = FakeReadinessClient(readiness())
    results = await CommerceReadinessService(client, cache_seconds=0).all_decisions(force=True)  # type: ignore[arg-type]

    assert len(results) == len(CommerceOperationKind)
    assert client.calls == 1
