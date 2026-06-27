from __future__ import annotations

from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest
from sqlalchemy import func, select

from vpn_access_bot.advertising_readiness import (
    AcquisitionService,
    CapacityService,
    CommercePolicyChangeError,
    CommercePolicyRepository,
)
from vpn_access_bot.config import Settings
from vpn_access_bot.db import Database
from vpn_access_bot.handlers.admin import (
    handle_commerce_start,
    handle_confirm_commerce,
)
from vpn_access_bot.handlers.common import handle_start
from vpn_access_bot.models import (
    AcquisitionTouch,
    CapacityStateTransition,
    CommercePolicyChangeRequest,
    ProductEvent,
    User,
    UserAcquisition,
    WorkerHealth,
    utc_now,
)
from vpn_access_bot.product_events import FUNNEL_EVENT_ORDER


def _settings(**overrides: object) -> Settings:
    values: dict[str, object] = {
        "TELEGRAM_BOT_TOKEN": "test-token",
        "ADMIN_TELEGRAM_IDS": "1",
        "MEDIATOR_ADMIN_TOKEN": "test-admin-token",
    }
    values.update(overrides)
    return Settings(**values)


@pytest.mark.asyncio
async def test_commerce_switch_is_durable_versioned_and_auditable(tmp_path: Path) -> None:
    database = Database(f"sqlite+aiosqlite:///{tmp_path / 'policy.db'}")
    await database.initialize()
    try:
        async with database.session() as session:
            repository = CommercePolicyRepository(session)
            policy = await repository.get()
            initial_version = policy.version
            policy = await repository.set_switch(
                switch_name="new_purchases",
                enabled=False,
                admin_telegram_id=101,
                reason_code="campaign_stop",
                operator_note="refund rate threshold",
            )
            await session.commit()
            assert policy.version == initial_version + 1

        async with database.session() as session:
            policy = await CommercePolicyRepository(session).get()
            assert policy.new_purchases_enabled is False
            assert policy.updated_by_admin_telegram_id == 101
            assert policy.reason_code == "campaign_stop"
            assert policy.operator_note == "refund rate threshold"
    finally:
        await database.dispose()


@pytest.mark.asyncio
async def test_campaign_first_touch_is_immutable_and_duplicate_start_is_idempotent(
    tmp_path: Path,
) -> None:
    database = Database(f"sqlite+aiosqlite:///{tmp_path / 'acquisition.db'}")
    await database.initialize()
    try:
        async with database.session() as session:
            user = User(
                telegram_id=501,
                username="campaign-user",
                first_name="Campaign",
                created_at=utc_now(),
                updated_at=utc_now(),
            )
            session.add(user)
            await session.flush()
            service = AcquisitionService(session)
            first = await service.create_campaign(channel="channel-a", placement="post-1")
            second = await service.create_campaign(channel="channel-b", placement="post-2")
            await session.commit()
            user_id = user.id
            first_id = first.id
            second_id = second.id
            first_payload = f"c_{first.public_token}"
            second_payload = f"c_{second.public_token}"

        async with database.session() as session:
            service = AcquisitionService(session)
            assert (
                await service.record_start(
                    user_id=user_id,
                    payload=first_payload,
                    bot_key="primary",
                )
                is not None
            )
            assert (
                await service.record_start(
                    user_id=user_id,
                    payload=first_payload,
                    bot_key="primary",
                )
                is not None
            )
            assert (
                await service.record_start(
                    user_id=user_id,
                    payload=second_payload,
                    bot_key="primary",
                )
                is not None
            )
            # Referral payloads and unknown tokens are not campaign attribution.
            assert (
                await service.record_start(
                    user_id=user_id,
                    payload="ref_12345",
                    bot_key="primary",
                )
                is None
            )
            assert (
                await service.record_start(
                    user_id=user_id,
                    payload="c_missing",
                    bot_key="primary",
                )
                is None
            )
            await session.commit()

        async with database.session() as session:
            acquisition = await session.get(UserAcquisition, user_id)
            assert acquisition is not None
            assert acquisition.first_campaign_id == first_id
            assert acquisition.last_campaign_id == second_id
            touch_count = await session.scalar(
                select(func.count(AcquisitionTouch.id)).where(AcquisitionTouch.user_id == user_id)
            )
            assert touch_count == 2
    finally:
        await database.dispose()


@pytest.mark.asyncio
async def test_capacity_is_unknown_without_limits_and_saturates_at_high_watermark(
    tmp_path: Path,
) -> None:
    database = Database(f"sqlite+aiosqlite:///{tmp_path / 'capacity.db'}")
    await database.initialize()
    try:
        async with database.session() as session:
            unknown = await CapacityService(session, _settings()).capture()
            assert unknown.state == "unknown"
            assert unknown.reason_code == "capacity_not_configured"

        async with database.session() as session:
            for index in range(9):
                user = User(
                    telegram_id=600 + index,
                    username=f"capacity-{index}",
                    first_name="Capacity",
                    created_at=utc_now(),
                    updated_at=utc_now(),
                )
                session.add(user)
                await session.flush()
                # Raw SQL keeps this test focused on capacity accounting rather than tariff setup.
                await session.execute(
                    __import__("sqlalchemy").text(
                        """
                        INSERT INTO subscriptions(
                            user_id, public_guid, signed_url, max_devices, status,
                            starts_at, expires_at, created_at, updated_at_utc
                        ) VALUES(
                            :user_id, :guid, '', 1, 'active', datetime('now'),
                            datetime('now', '+30 days'), datetime('now'), datetime('now')
                        )
                        """
                    ),
                    {"user_id": user.id, "guid": f"00000000-0000-0000-0000-{index:012d}"},
                )
            await session.commit()

        async with database.session() as session:
            snapshot = await CapacityService(
                session,
                _settings(
                    CONFIGURED_SUBSCRIPTION_CAPACITY=10,
                    CAPACITY_CONSTRAINED_RATIO=0.70,
                    CAPACITY_SATURATED_RATIO=0.85,
                ),
            ).capture()
            assert snapshot.active_subscriptions == 9
            assert snapshot.state == "saturated"
            assert snapshot.reason_code == "capacity_high_watermark"
    finally:
        await database.dispose()


@pytest.mark.asyncio
async def test_capacity_saturates_when_critical_worker_is_stale(tmp_path: Path) -> None:
    database = Database(f"sqlite+aiosqlite:///{tmp_path / 'capacity-stale-worker.db'}")
    await database.initialize()
    try:
        async with database.session() as session:
            session.add(
                WorkerHealth(
                    worker_name="payment_reconciliation",
                    last_attempt_at_utc=utc_now() - timedelta(hours=2),
                    last_success_at_utc=utc_now() - timedelta(hours=2),
                    last_failure_at_utc=None,
                    last_error_code=None,
                )
            )
            await session.commit()

        async with database.session() as session:
            snapshot = await CapacityService(
                session,
                _settings(
                    CONFIGURED_SUBSCRIPTION_CAPACITY=10,
                    WORKER_STALE_AFTER_SECONDS=60,
                    CAPACITY_WORKER_STALE_LIMIT=1,
                ),
            ).capture()

            assert snapshot.worker_stale_count == 1
            assert snapshot.state == "saturated"
            assert snapshot.reason_code == "critical_worker_stale"
    finally:
        await database.dispose()


@pytest.mark.asyncio
async def test_capacity_hysteresis_prevents_rapid_reopening(tmp_path: Path) -> None:
    database = Database(f"sqlite+aiosqlite:///{tmp_path / 'capacity-hysteresis.db'}")
    await database.initialize()
    settings = _settings(
        CONFIGURED_SUBSCRIPTION_CAPACITY=10,
        CAPACITY_CONSTRAINED_RATIO=0.70,
        CAPACITY_SATURATED_RATIO=0.85,
        CAPACITY_RECOVERY_RATIO=0.65,
        CAPACITY_MIN_DWELL_SECONDS=3600,
    )
    try:
        async with database.session() as session:
            for index in range(9):
                user = User(
                    telegram_id=800 + index,
                    username=f"hysteresis-{index}",
                    first_name="Hysteresis",
                    created_at=utc_now(),
                    updated_at=utc_now(),
                )
                session.add(user)
                await session.flush()
                await session.execute(
                    __import__("sqlalchemy").text(
                        """
                        INSERT INTO subscriptions(
                            user_id, public_guid, signed_url, max_devices, status,
                            starts_at, expires_at, created_at, updated_at_utc
                        ) VALUES(
                            :user_id, :guid, '', 1, 'active', datetime('now'),
                            datetime('now', '+30 days'), datetime('now'), datetime('now')
                        )
                        """
                    ),
                    {"user_id": user.id, "guid": f"10000000-0000-0000-0000-{index:012d}"},
                )
            saturated = await CapacityService(session, settings).capture()
            assert saturated.state == "saturated"
            await session.commit()

        async with database.session() as session:
            await session.execute(
                __import__("sqlalchemy").text(
                    "UPDATE subscriptions SET status = 'disabled' WHERE id > 5"
                )
            )
            held = await CapacityService(session, settings).capture()
            assert held.active_subscriptions == 5
            assert held.state == "saturated"
            assert held.reason_code == "capacity_hysteresis_hold"
            await session.commit()

        async with database.session() as session:
            transition = (
                await session.execute(
                    select(CapacityStateTransition)
                    .order_by(CapacityStateTransition.captured_at_utc.desc())
                    .limit(1)
                )
            ).scalar_one()
            transition.captured_at_utc = utc_now() - timedelta(hours=2)
            await session.commit()

        async with database.session() as session:
            recovered = await CapacityService(session, settings).capture()
            assert recovered.state == "healthy"
            assert recovered.reason_code == "within_capacity"
            await session.commit()
    finally:
        await database.dispose()


@pytest.mark.asyncio
async def test_campaign_tracking_switch_stops_new_attribution(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    database = Database(f"sqlite+aiosqlite:///{tmp_path / 'tracking-switch.db'}")
    await database.initialize()
    settings = _settings()
    try:
        async with database.session() as session:
            campaign = await AcquisitionService(session).create_campaign(channel="disabled")
            policy = await CommercePolicyRepository(session).get()
            policy.campaign_tracking_enabled = False
            token = campaign.public_token
            await session.commit()

        async def ignore_menu(*args, **kwargs) -> None:
            del args, kwargs

        monkeypatch.setattr(
            "vpn_access_bot.handlers.common._answer_main_menu",
            ignore_menu,
        )
        message = SimpleNamespace(
            text=f"/start c_{token}",
            from_user=SimpleNamespace(
                id=909001,
                username="tracking-disabled",
                first_name="Tracking",
            ),
        )
        await handle_start(
            message,
            database,
            settings,
            object(),
            object(),
        )

        async with database.session() as session:
            acquisition_count = await session.scalar(select(func.count(UserAcquisition.user_id)))
            event_count = await session.scalar(select(func.count(ProductEvent.id)))
            assert acquisition_count == 0
            assert event_count == 1
    finally:
        await database.dispose()


class _AdminMessage:
    def __init__(self, text: str, admin_id: int = 1) -> None:
        self.text = text
        self.from_user = SimpleNamespace(id=admin_id)
        self.answers: list[str] = []

    async def answer(self, text: str, **kwargs: object) -> None:
        del kwargs
        self.answers.append(text)


@pytest.mark.asyncio
async def test_new_database_starts_with_conservative_commerce_policy(tmp_path: Path) -> None:
    database = Database(f"sqlite+aiosqlite:///{tmp_path / 'conservative-policy.db'}")
    await database.initialize()
    try:
        async with database.session() as session:
            policy = await CommercePolicyRepository(session).get()
            assert policy.new_purchases_enabled is False
            assert policy.trials_enabled is False
            assert policy.renewals_enabled is True
            assert policy.resumes_enabled is False
            assert policy.device_upgrades_enabled is False
            assert policy.referrals_enabled is False
            assert policy.reason_code == "pre_advertising_freeze"
    finally:
        await database.dispose()


@pytest.mark.asyncio
async def test_commerce_switch_requires_restart_safe_one_time_confirmation(
    tmp_path: Path,
) -> None:
    database = Database(f"sqlite+aiosqlite:///{tmp_path / 'confirmed-policy.db'}")
    await database.initialize()
    settings = _settings()
    try:
        prepare = _AdminMessage("/commerce_start new_purchases canary_open")
        await handle_commerce_start(prepare, database, settings)
        assert prepare.answers
        token = prepare.answers[-1].split("/confirm_commerce ", 1)[1].split("</code>", 1)[0]

        async with database.session() as session:
            policy = await CommercePolicyRepository(session).get()
            assert policy.new_purchases_enabled is False
            request = (await session.execute(select(CommercePolicyChangeRequest))).scalar_one()
            assert request.state == "pending"
            assert request.requested_enabled is True

        confirm = _AdminMessage(f"/confirm_commerce {token}")
        await handle_confirm_commerce(confirm, database, settings)
        assert "включён" in confirm.answers[-1]

        async with database.session() as session:
            request = (await session.execute(select(CommercePolicyChangeRequest))).scalar_one()
            assert request.state == "confirmed"
            assert request.confirmed_at_utc is not None
            policy = await CommercePolicyRepository(session).get()
            assert policy.new_purchases_enabled is True

        repeated = _AdminMessage(f"/confirm_commerce {token}")
        await handle_confirm_commerce(repeated, database, settings)
        assert "уже использован" in repeated.answers[-1]
    finally:
        await database.dispose()


@pytest.mark.asyncio
async def test_stale_commerce_confirmation_cannot_overwrite_newer_policy(
    tmp_path: Path,
) -> None:
    database = Database(f"sqlite+aiosqlite:///{tmp_path / 'stale-policy.db'}")
    await database.initialize()
    try:
        async with database.session() as session:
            repository = CommercePolicyRepository(session)
            preview = await repository.prepare_switch_change(
                switch_name="new_purchases",
                enabled=True,
                admin_telegram_id=1,
                reason_code="open_canary",
            )
            await session.commit()

        async with database.session() as session:
            repository = CommercePolicyRepository(session)
            policy = await repository.get()
            await repository.set_switch(
                switch_name="trials",
                enabled=False,
                admin_telegram_id=2,
                reason_code="separate_operator_change",
                expected_version=policy.version,
            )
            await session.commit()

        async with database.session() as session:
            repository = CommercePolicyRepository(session)
            with pytest.raises(
                CommercePolicyChangeError,
                match="commerce_policy_version_conflict",
            ):
                await repository.confirm_switch_change(
                    confirmation_token=preview.confirmation_token,
                    admin_telegram_id=1,
                )
            await session.commit()

        async with database.session() as session:
            policy = await CommercePolicyRepository(session).get()
            assert policy.new_purchases_enabled is False
            request = (await session.execute(select(CommercePolicyChangeRequest))).scalar_one()
            assert request.state == "stale"
            assert request.failure_code == "commerce_policy_version_conflict"
    finally:
        await database.dispose()


def test_every_funnel_event_has_a_real_producer_in_application_code() -> None:
    package_root = Path(__file__).resolve().parents[1] / "vpn_access_bot"
    producer_text = "\n".join(
        path.read_text(encoding="utf-8")
        for path in package_root.rglob("*.py")
        if path.name != "product_events.py"
    )
    missing = [event.value for event in FUNNEL_EVENT_ORDER if event.value not in producer_text]
    assert missing == []
    assert "credential_created" not in {event.value for event in FUNNEL_EVENT_ORDER}
    assert "device_limit_reached" not in {event.value for event in FUNNEL_EVENT_ORDER}


@pytest.mark.asyncio
async def test_extend_and_upgrade_switch_is_independent_and_defaults_closed(tmp_path: Path) -> None:
    database = Database(f"sqlite+aiosqlite:///{tmp_path / 'split-commerce-switch.db'}")
    await database.initialize()
    try:
        async with database.session() as session:
            repository = CommercePolicyRepository(session)
            policy = await repository.get()
            assert policy.device_upgrades_enabled is False
            assert policy.extend_and_upgrade_enabled is False

            policy = await repository.set_switch(
                switch_name="device_upgrades",
                enabled=True,
                admin_telegram_id=101,
                reason_code="ordinary_upgrade_canary",
            )
            await session.commit()
            assert policy.device_upgrades_enabled is True
            assert policy.extend_and_upgrade_enabled is False

        async with database.session() as session:
            policy = await CommercePolicyRepository(session).set_switch(
                switch_name="extend_and_upgrade",
                enabled=True,
                admin_telegram_id=101,
                reason_code="combined_flow_canary",
            )
            await session.commit()
            assert policy.device_upgrades_enabled is True
            assert policy.extend_and_upgrade_enabled is True
    finally:
        await database.dispose()
