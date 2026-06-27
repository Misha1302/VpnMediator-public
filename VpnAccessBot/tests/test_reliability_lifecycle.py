from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from itertools import product
from pathlib import Path

import pytest
from sqlalchemy import func, select

from vpn_access_bot.config import Settings
from vpn_access_bot.constants import (
    ENTITLEMENT_STATUS_ACTIVE,
    ENTITLEMENT_STATUS_DISABLED,
    ENTITLEMENT_STATUS_EXPIRED,
    ORDER_KIND_RESUME,
    SUBSCRIPTION_STATUS_DISABLED,
    SUBSCRIPTION_STATUS_EXPIRED,
    TRIAL_DURATION_SECONDS,
    TRIAL_STATUS_ACTIVE,
    TRIAL_STATUS_EXPIRED,
    TRIAL_STATUS_REVOKED,
)
from vpn_access_bot.db import Database
from vpn_access_bot.mediator_client import (
    MediatorClientError,
    MediatorEntitlementDetails,
    MediatorEntitlementOperationResult,
)
from vpn_access_bot.models import (
    AccessEntitlement,
    AccessOperationLease,
    EntitlementOperation,
    NotificationOutbox,
    Order,
    OrderApplication,
    PaymentInbox,
    RefundOperation,
    Subscription,
    TrialClaim,
    User,
    utc_now,
)
from vpn_access_bot.operations import (
    EntitlementOperationCoordinator,
    EntitlementRecoveryWorker,
)
from vpn_access_bot.product_completion import (
    reconcile_entitlements_once,
    recover_legacy_activating_orders_once,
    recover_refund_operations_once,
)
from vpn_access_bot.repositories import (
    AccessOperationLeaseRepository,
    EntitlementOperationRepository,
    EntitlementRepository,
    NotificationOutboxRepository,
    PaymentInboxRepository,
    RefundOperationRepository,
    TrialClaimRepository,
    to_aware_utc,
)
from vpn_access_bot.services import (
    AdminEntitlementAdjustmentService,
    ExpirationService,
    PurchaseService,
    ReconciliationRepairService,
    run_expiration_worker,
)


@pytest.mark.asyncio
async def test_expiration_worker_runs_before_first_interval_sleep(monkeypatch) -> None:
    expiration_calls: list[str] = []
    sleep_calls: list[int] = []

    class FakeExpirationService:
        def __init__(self, session, mediator_client) -> None:
            del session, mediator_client

        async def expire_due_subscriptions(self) -> int:
            expiration_calls.append("run")
            return 0

    @asynccontextmanager
    async def session_factory():
        yield object()

    async def stop_after_first_sleep(seconds: int) -> None:
        sleep_calls.append(seconds)
        raise asyncio.CancelledError

    monkeypatch.setattr(
        "vpn_access_bot.services.ExpirationService",
        FakeExpirationService,
    )
    monkeypatch.setattr(
        "vpn_access_bot.services.asyncio.sleep",
        stop_after_first_sleep,
    )

    with pytest.raises(asyncio.CancelledError):
        await run_expiration_worker(
            session_factory=session_factory,
            mediator_client=object(),
            interval_seconds=1800,
        )

    assert expiration_calls == ["run"]
    assert sleep_calls == [1800]


@dataclass
class DurableFakeMediator:
    public_guid: str
    version: int
    status: str
    valid_until_utc: datetime
    max_device_tokens: int
    operations: dict[str, MediatorEntitlementOperationResult] = field(default_factory=dict)
    apply_calls: int = 0
    last_requested_valid_until: datetime | None = None

    async def get_entitlement(self, public_guid: str) -> MediatorEntitlementDetails:
        assert public_guid == self.public_guid
        return MediatorEntitlementDetails(
            public_guid=public_guid,
            version=self.version,
            status=self.status,
            valid_until_utc=self.valid_until_utc.isoformat(),
            max_device_tokens=self.max_device_tokens,
            updated_at_utc=utc_now().isoformat(),
        )

    async def get_entitlement_operation(
        self, operation_id: str
    ) -> MediatorEntitlementOperationResult | None:
        return self.operations.get(operation_id)

    async def get_entitlement_operation_by_result_version(
        self, public_guid: str, result_version: int
    ) -> MediatorEntitlementOperationResult | None:
        assert public_guid == self.public_guid
        return next(
            (
                item
                for item in self.operations.values()
                if item.public_guid == public_guid and item.result_version == result_version
            ),
            None,
        )

    async def apply_entitlement_operation(
        self,
        public_guid: str,
        *,
        operation_id: str,
        operation_type: str,
        expected_version: int,
        status: str,
        valid_until_utc: str,
        max_device_tokens: int,
    ) -> MediatorEntitlementOperationResult:
        assert public_guid == self.public_guid
        existing = self.operations.get(operation_id)
        if existing is not None:
            return existing
        if expected_version != self.version:
            raise MediatorClientError(
                "version conflict",
                error_code="entitlement_operation_version_conflict",
            )
        self.apply_calls += 1
        self.version += 1
        self.status = status
        self.valid_until_utc = to_aware_utc(datetime.fromisoformat(valid_until_utc))
        self.max_device_tokens = max_device_tokens
        self.last_requested_valid_until = self.valid_until_utc
        result = MediatorEntitlementOperationResult(
            status="applied",
            operation_id=operation_id,
            public_guid=public_guid,
            operation_type=operation_type,
            expected_version=expected_version,
            result_version=self.version,
            result_status=status,
            result_valid_until_utc=self.valid_until_utc.isoformat(),
            result_max_device_tokens=max_device_tokens,
            applied_at_utc=utc_now().isoformat(),
        )
        self.operations[operation_id] = result
        return result


@dataclass
class ChangingSnapshotMediator(DurableFakeMediator):
    change_on_read: int = 2
    read_count: int = 0

    async def get_entitlement(self, public_guid: str) -> MediatorEntitlementDetails:
        self.read_count += 1
        if self.read_count == self.change_on_read:
            self.version += 1
        return await super().get_entitlement(public_guid)


async def _seed_subscription(
    database: Database,
    *,
    expires_at: datetime,
    status: str = ENTITLEMENT_STATUS_ACTIVE,
    entitlement_status: str | None = None,
    entitlement_version: int = 1,
    max_devices: int = 3,
) -> tuple[int, int, str]:
    async with database.session() as session:
        user = User(
            telegram_id=900001,
            username="reliability-user",
            first_name="Reliability",
            created_at=utc_now(),
            updated_at=utc_now(),
        )
        session.add(user)
        await session.flush()
        public_guid = "00000000-0000-0000-0000-000000000901"
        subscription = Subscription(
            user_id=user.id,
            public_guid=public_guid,
            signed_url="",
            max_devices=max_devices,
            status=status,
            starts_at=expires_at - timedelta(days=30),
            expires_at=expires_at,
            created_at=expires_at - timedelta(days=30),
            updated_at_utc=utc_now(),
        )
        session.add(subscription)
        await session.flush()
        user.primary_subscription_id = subscription.id
        session.add(
            AccessEntitlement(
                subscription_id=subscription.id,
                version=entitlement_version,
                status=entitlement_status or status,
                valid_until_utc=expires_at,
                max_device_tokens=max_devices,
                updated_at_utc=utc_now(),
            )
        )
        await session.flush()
        return user.id, subscription.id, public_guid


async def _seed_active_trial(
    database: Database,
    *,
    user_id: int,
    subscription_id: int,
    started_at: datetime,
    ends_at: datetime,
) -> None:
    async with database.session() as session:
        session.add(
            TrialClaim(
                user_id=user_id,
                subscription_id=subscription_id,
                status=TRIAL_STATUS_ACTIVE,
                duration_seconds=int((ends_at - started_at).total_seconds()),
                max_devices=1,
                started_at_utc=started_at,
                ends_at_utc=ends_at,
                entitlement_version=1,
                idempotency_key=f"trial-test:{user_id}",
                created_at_utc=started_at,
                reserved_at_utc=started_at,
                usable_started_at_utc=started_at,
                activated_at_utc=started_at,
            )
        )


@pytest.mark.asyncio
async def test_vm001_expiration_updates_authoritative_mirror_and_resume_can_advance(
    tmp_path: Path,
) -> None:
    database = Database(f"sqlite+aiosqlite:///{tmp_path / 'vm001.db'}")
    await database.initialize()
    expired_at = utc_now() - timedelta(minutes=5)
    user_id, subscription_id, public_guid = await _seed_subscription(
        database, expires_at=expired_at
    )
    await _seed_active_trial(
        database,
        user_id=user_id,
        subscription_id=subscription_id,
        started_at=expired_at - timedelta(days=2),
        ends_at=expired_at,
    )
    mediator = DurableFakeMediator(
        public_guid,
        1,
        ENTITLEMENT_STATUS_ACTIVE,
        expired_at,
        3,
    )
    try:
        async with database.session() as session:
            expired = await ExpirationService(session, mediator).expire_due_subscriptions()
            assert expired == 1

        async with database.session() as session:
            subscription = await session.get(Subscription, subscription_id)
            entitlement = (
                await session.execute(
                    select(AccessEntitlement).where(
                        AccessEntitlement.subscription_id == subscription_id
                    )
                )
            ).scalar_one()
            assert subscription is not None
            claim = await TrialClaimRepository(session).get_for_user(user_id)
            assert subscription.status == "expired"
            assert entitlement.version == 2
            assert entitlement.status == ENTITLEMENT_STATUS_EXPIRED
            assert claim is not None
            assert claim.status == TRIAL_STATUS_EXPIRED
            assert claim.expired_at_utc is not None
            assert claim.revoked_at_utc is None

            coordinator = EntitlementOperationCoordinator(session, mediator, worker_id="resume")
            operation = await coordinator.prepare_generic(
                user_id=subscription.user_id,
                subscription_id=subscription.id,
                operation_type="paid_activation",
                source_entity_type="order",
                source_entity_id="resume-order",
                duration_delta_seconds=30 * 86400,
                requested_device_limit=3,
                requested_status=ENTITLEMENT_STATUS_ACTIVE,
                observed_valid_until_utc=expired_at,
            )
            await session.commit()
            applied = await coordinator.apply_generic(operation, subscription)
            assert applied is not None
            assert applied.version == 3
            assert applied.status == ENTITLEMENT_STATUS_ACTIVE
            assert applied.valid_until_utc > expired_at
    finally:
        await database.dispose()


@pytest.mark.asyncio
async def test_vm002_restart_recovers_remote_apply_without_reapplying_delta(
    tmp_path: Path,
) -> None:
    path = tmp_path / "vm002.db"
    database = Database(f"sqlite+aiosqlite:///{path}")
    await database.initialize()
    expires_at = utc_now() + timedelta(days=10)
    user_id, subscription_id, public_guid = await _seed_subscription(
        database, expires_at=expires_at
    )
    operation_public_id: str
    async with database.session() as session:
        repository = EntitlementOperationRepository(session)
        operation = await repository.create_once(
            user_id=user_id,
            subscription_id=subscription_id,
            operation_type="paid_activation",
            source_entity_type="order",
            source_entity_id="stale-activating-order",
            idempotency_key="entitlement:order:stale-activating-order",
            duration_delta_seconds=30 * 86400,
            requested_device_limit=3,
            requested_status=ENTITLEMENT_STATUS_ACTIVE,
            observed_valid_until_utc=expires_at,
        )
        await repository.mark_request_sent(operation)
        operation_public_id = operation.public_id
    await database.dispose()

    result_until = expires_at + timedelta(days=30)
    mediator = DurableFakeMediator(
        public_guid,
        2,
        ENTITLEMENT_STATUS_ACTIVE,
        result_until,
        3,
    )
    mediator.operations[operation_public_id] = MediatorEntitlementOperationResult(
        status="applied",
        operation_id=operation_public_id,
        public_guid=public_guid,
        operation_type="paid_activation",
        expected_version=1,
        result_version=2,
        result_status=ENTITLEMENT_STATUS_ACTIVE,
        result_valid_until_utc=result_until.isoformat(),
        result_max_device_tokens=3,
        applied_at_utc=utc_now().isoformat(),
    )

    restarted = Database(f"sqlite+aiosqlite:///{path}")
    await restarted.initialize()
    try:
        async with restarted.session() as session:
            operation = await EntitlementOperationRepository(session).get_by_public_id(
                operation_public_id
            )
            assert operation is not None
            state = await EntitlementRecoveryWorker(session, mediator).classify(operation)
            assert state == "external_applied"
            assert operation.external_result_version == 2
            assert operation.external_result_valid_until_utc == result_until
            assert mediator.apply_calls == 0
    finally:
        await restarted.dispose()


@pytest.mark.asyncio
async def test_vm003_refund_serialization_blocks_renewal_and_lease_theft(
    tmp_path: Path,
) -> None:
    database = Database(f"sqlite+aiosqlite:///{tmp_path / 'vm003.db'}")
    await database.initialize()
    expires_at = utc_now() + timedelta(days=10)
    user_id, subscription_id, public_guid = await _seed_subscription(
        database, expires_at=expires_at
    )
    mediator = DurableFakeMediator(public_guid, 1, ENTITLEMENT_STATUS_ACTIVE, expires_at, 3)
    try:
        async with database.session() as session:
            order = Order(
                public_order_id="refund-order",
                user_id=user_id,
                target_subscription_id=subscription_id,
                order_kind="extend",
                status="refunding",
                amount_minor_units=199,
                currency="XTR",
                provider="telegram_stars",
                provider_payment_id="provider-charge-refund",
                invoice_payload="refund-payload",
                paid_at=utc_now(),
                created_at=utc_now(),
            )
            session.add(order)
            await session.flush()
            refund = await RefundOperationRepository(session).create_once(
                order=order,
                subscription_id=subscription_id,
                provider_charge_id=order.provider_payment_id,
            )
            lease = AccessOperationLeaseRepository(session)
            assert await lease.acquire(
                user_id=user_id,
                owner_kind="refund",
                owner_key=f"refund:{order.public_order_id}",
                lease_seconds=3600,
            )
            assert not await lease.acquire(
                user_id=user_id,
                owner_kind="order",
                owner_key="order:competing-renewal",
            )
            assert refund.state == "prepared"
            with pytest.raises(MediatorClientError) as error:
                await EntitlementOperationCoordinator(session, mediator).prepare_generic(
                    user_id=user_id,
                    subscription_id=subscription_id,
                    operation_type="paid_activation",
                    source_entity_type="order",
                    source_entity_id="competing-renewal",
                    duration_delta_seconds=86400,
                    requested_device_limit=3,
                    requested_status=ENTITLEMENT_STATUS_ACTIVE,
                )
            assert error.value.error_code == "refund_operation_in_progress"
    finally:
        await database.dispose()


@pytest.mark.asyncio
async def test_vm003_stale_unknown_refund_is_quarantined_after_restart(
    tmp_path: Path,
) -> None:
    database = Database(f"sqlite+aiosqlite:///{tmp_path / 'vm003-refund-recovery.db'}")
    await database.initialize()
    expires_at = utc_now() + timedelta(days=10)
    user_id, subscription_id, public_guid = await _seed_subscription(
        database, expires_at=expires_at
    )
    mediator = DurableFakeMediator(public_guid, 1, ENTITLEMENT_STATUS_ACTIVE, expires_at, 3)
    settings = Settings(
        TELEGRAM_BOT_TOKEN="test-token",
        ADMIN_TELEGRAM_IDS="1",
        MEDIATOR_ADMIN_TOKEN="test-admin-token",
    )
    try:
        async with database.session() as session:
            order = Order(
                public_order_id="refund-unknown-order",
                user_id=user_id,
                target_subscription_id=subscription_id,
                order_kind="extend",
                status="refunding",
                amount_minor_units=199,
                currency="XTR",
                provider="telegram_stars",
                provider_payment_id="provider-charge-unknown",
                invoice_payload="refund-unknown-payload",
                paid_at=utc_now(),
                created_at=utc_now(),
            )
            session.add(order)
            await session.flush()
            repository = RefundOperationRepository(session)
            operation = await repository.create_once(
                order=order,
                subscription_id=subscription_id,
                provider_charge_id=order.provider_payment_id,
            )
            await repository.mark_provider_requested(operation)
            operation.updated_at_utc = utc_now() - timedelta(minutes=30)
            assert await AccessOperationLeaseRepository(session).acquire(
                user_id=user_id,
                owner_kind="refund",
                owner_key=f"refund:{order.public_order_id}",
                lease_seconds=3600,
            )

        recovered = await recover_refund_operations_once(database.session, mediator, settings)
        assert recovered == 0

        async with database.session() as session:
            operation = await RefundOperationRepository(session).get_for_order(order.id)
            assert operation is not None
            assert operation.state == "manual_review"
            assert operation.last_error_code == "provider_outcome_unknown_after_restart"
            alerts = await session.scalar(
                select(func.count(NotificationOutbox.id)).where(
                    NotificationOutbox.notification_kind == "operator_refund_unknown_alert"
                )
            )
            leases = await session.scalar(select(func.count(AccessOperationLease.user_id)))
            assert alerts == 1
            assert leases == 0
    finally:
        await database.dispose()


@pytest.mark.asyncio
async def test_vm004_purchased_delta_uses_remote_newer_entitlement_exactly_once(
    tmp_path: Path,
) -> None:
    database = Database(f"sqlite+aiosqlite:///{tmp_path / 'vm004.db'}")
    await database.initialize()
    local_until = utc_now() + timedelta(days=10)
    user_id, subscription_id, public_guid = await _seed_subscription(
        database, expires_at=local_until
    )
    remote_until = local_until + timedelta(days=20)
    mediator = DurableFakeMediator(public_guid, 5, ENTITLEMENT_STATUS_ACTIVE, remote_until, 6)
    try:
        async with database.session() as session:
            subscription = await session.get(Subscription, subscription_id)
            assert subscription is not None
            coordinator = EntitlementOperationCoordinator(session, mediator, worker_id="delta")
            operation = await coordinator.prepare_generic(
                user_id=user_id,
                subscription_id=subscription_id,
                operation_type="paid_activation",
                source_entity_type="order",
                source_entity_id="old-quote-new-remote",
                duration_delta_seconds=30 * 86400,
                requested_device_limit=3,
                requested_status=ENTITLEMENT_STATUS_ACTIVE,
                observed_valid_until_utc=local_until,
            )
            await session.commit()
            first = await coordinator.apply_generic(operation, subscription)
            replay = await coordinator.apply_generic(operation, subscription)
            assert first is not None and replay is not None
            assert first.valid_until_utc == remote_until + timedelta(days=30)
            assert replay.valid_until_utc == first.valid_until_utc
            assert first.max_device_tokens == 6
            assert mediator.apply_calls == 1
    finally:
        await database.dispose()


@pytest.mark.asyncio
async def test_vm005_payment_inbox_is_first_durable_boundary_and_detects_conflicts(
    tmp_path: Path,
) -> None:
    path = tmp_path / "vm005.db"
    database = Database(f"sqlite+aiosqlite:///{path}")
    await database.initialize()
    try:
        async with database.session() as session:
            repository = PaymentInboxRepository(session)
            inbox, inserted = await repository.receive(
                provider="telegram_stars",
                provider_charge_id="charge-durable-001",
                invoice_payload="unknown-order-payload",
                payer_external_id="900001",
                amount_minor_units=199,
                currency="XTR",
            )
            assert inserted
            await repository.mark_manual_review(inbox, "order_not_found")

        async with database.session() as session:
            repository = PaymentInboxRepository(session)
            replay, inserted = await repository.receive(
                provider="telegram_stars",
                provider_charge_id="charge-durable-001",
                invoice_payload="different-payload",
                payer_external_id="900001",
                amount_minor_units=199,
                currency="XTR",
            )
            assert not inserted
            assert replay.reconciliation_status == "manual_review"
            assert replay.failure_code == "provider_charge_evidence_conflict"
            count = await session.scalar(select(func.count(PaymentInbox.id)))
            assert count == 1
    finally:
        await database.dispose()


@pytest.mark.asyncio
async def test_vm006_trial_reservation_does_not_start_usable_period(
    tmp_path: Path,
) -> None:
    database = Database(f"sqlite+aiosqlite:///{tmp_path / 'vm006.db'}")
    await database.initialize()
    try:
        async with database.session() as session:
            user = User(
                telegram_id=900006,
                username="trial-user",
                first_name="Trial",
                created_at=utc_now(),
                updated_at=utc_now(),
            )
            session.add(user)
            await session.flush()
            claim, acquired = await TrialClaimRepository(session).acquire_activation(
                user,
                duration_seconds=TRIAL_DURATION_SECONDS,
                max_devices=1,
            )
            assert acquired and claim is not None
            assert claim.reserved_at_utc is not None
            assert claim.started_at_utc is None
            assert claim.usable_started_at_utc is None
            assert claim.ends_at_utc is None
            delayed_activation = utc_now() + timedelta(hours=5)
            claim.started_at_utc = delayed_activation
            claim.usable_started_at_utc = delayed_activation
            claim.ends_at_utc = delayed_activation + timedelta(seconds=claim.duration_seconds)
            claim.status = "active"
            await session.flush()
            assert claim.ends_at_utc - claim.usable_started_at_utc == timedelta(
                seconds=TRIAL_DURATION_SECONDS
            )
    finally:
        await database.dispose()


@pytest.mark.asyncio
async def test_operation_based_admin_adjustment_and_revoke_are_idempotent(
    tmp_path: Path,
) -> None:
    database = Database(f"sqlite+aiosqlite:///{tmp_path / 'admin-adjustment.db'}")
    await database.initialize()
    expires_at = utc_now() + timedelta(days=10)
    user_id, subscription_id, public_guid = await _seed_subscription(
        database, expires_at=expires_at
    )
    await _seed_active_trial(
        database,
        user_id=user_id,
        subscription_id=subscription_id,
        started_at=utc_now() - timedelta(hours=1),
        ends_at=utc_now() + timedelta(hours=47),
    )
    mediator = DurableFakeMediator(public_guid, 1, ENTITLEMENT_STATUS_ACTIVE, expires_at, 3)
    try:
        async with database.session() as session:
            service = AdminEntitlementAdjustmentService(session, mediator)
            first = await service.apply(
                public_guid=public_guid,
                actor_telegram_id=1,
                source_request_id="admin-message-1",
                reason="support_compensation",
                duration_days=5,
                requested_device_limit=5,
            )
            replay = await service.apply(
                public_guid=public_guid,
                actor_telegram_id=1,
                source_request_id="admin-message-1",
                reason="support_compensation",
                duration_days=5,
                requested_device_limit=5,
            )
            assert first.operation_public_id == replay.operation_public_id
            assert first.subscription.expires_at == expires_at + timedelta(days=5)
            assert first.subscription.max_devices == 5
            assert mediator.apply_calls == 1

        async with database.session() as session:
            revoked = await AdminEntitlementAdjustmentService(session, mediator).apply(
                public_guid=public_guid,
                actor_telegram_id=1,
                source_request_id="admin-message-2",
                reason="confirmed_abuse",
                disable=True,
            )
            assert revoked.status == ENTITLEMENT_STATUS_DISABLED
            assert revoked.subscription.status == "disabled"
            assert mediator.apply_calls == 2

        async with database.session() as session:
            operations = await session.scalar(select(func.count(EntitlementOperation.id)))
            outbox = await session.scalar(select(func.count(NotificationOutbox.id)))
            subscription = await session.get(Subscription, subscription_id)
            claim = await TrialClaimRepository(session).get_for_user(user_id)
            assert operations == 2
            assert outbox == 2
            assert subscription is not None and subscription.status == "disabled"
            assert claim is not None
            assert claim.status == TRIAL_STATUS_REVOKED
            assert claim.revoked_at_utc is not None
            assert claim.expired_at_utc is None
    finally:
        await database.dispose()


@pytest.mark.asyncio
async def test_vm008_unknown_remote_newer_is_quarantined_and_alerted(
    tmp_path: Path,
) -> None:
    database = Database(f"sqlite+aiosqlite:///{tmp_path / 'vm008.db'}")
    await database.initialize()
    expires_at = utc_now() + timedelta(days=10)
    _, subscription_id, public_guid = await _seed_subscription(database, expires_at=expires_at)
    mediator = DurableFakeMediator(
        public_guid,
        2,
        ENTITLEMENT_STATUS_ACTIVE,
        expires_at + timedelta(days=5),
        3,
    )
    try:
        synchronized = await reconcile_entitlements_once(
            database.session,
            mediator,
        )
        assert synchronized == 0
        async with database.session() as session:
            subscription = await session.get(Subscription, subscription_id)
            assert subscription is not None
            assert subscription.reconciliation_state == "blocked"
            assert subscription.reconciliation_reason == "remote_newer_unknown_origin"
            alerts = (
                (
                    await session.execute(
                        select(NotificationOutbox).where(
                            NotificationOutbox.notification_kind
                            == "operator_reconciliation_blocked"
                        )
                    )
                )
                .scalars()
                .all()
            )
            assert len(alerts) == 1
    finally:
        await database.dispose()


@pytest.mark.asyncio
async def test_matching_expired_active_snapshot_waits_for_expiration_without_quarantine(
    tmp_path: Path,
) -> None:
    database = Database(f"sqlite+aiosqlite:///{tmp_path / 'expiration-pending.db'}")
    await database.initialize()
    expired_at = utc_now() - timedelta(minutes=5)
    _, subscription_id, public_guid = await _seed_subscription(
        database,
        expires_at=expired_at,
    )
    mediator = DurableFakeMediator(
        public_guid,
        1,
        ENTITLEMENT_STATUS_ACTIVE,
        expired_at,
        3,
    )
    try:
        synchronized = await reconcile_entitlements_once(database.session, mediator)
        assert synchronized == 1

        async with database.session() as session:
            subscription = await session.get(Subscription, subscription_id)
            alerts = (
                (
                    await session.execute(
                        select(NotificationOutbox).where(
                            NotificationOutbox.notification_kind
                            == "operator_reconciliation_blocked"
                        )
                    )
                )
                .scalars()
                .all()
            )
            assert subscription is not None
            assert subscription.reconciliation_state == "healthy"
            assert subscription.reconciliation_reason is None
            assert alerts == []

        async with database.session() as session:
            expired = await ExpirationService(session, mediator).expire_due_subscriptions()
            assert expired == 1

        async with database.session() as session:
            subscription = await session.get(Subscription, subscription_id)
            entitlement = (
                await session.execute(
                    select(AccessEntitlement).where(
                        AccessEntitlement.subscription_id == subscription_id
                    )
                )
            ).scalar_one()
            assert subscription is not None
            assert subscription.status == SUBSCRIPTION_STATUS_EXPIRED
            assert entitlement.status == ENTITLEMENT_STATUS_EXPIRED
    finally:
        await database.dispose()


@pytest.mark.asyncio
async def test_legacy_expiration_drift_is_classified_and_explained_to_operator(
    tmp_path: Path,
) -> None:
    database = Database(f"sqlite+aiosqlite:///{tmp_path / 'legacy-drift.db'}")
    await database.initialize()
    expired_at = utc_now() - timedelta(minutes=5)
    _, subscription_id, public_guid = await _seed_subscription(
        database,
        expires_at=expired_at,
        status=SUBSCRIPTION_STATUS_EXPIRED,
        entitlement_status=ENTITLEMENT_STATUS_ACTIVE,
    )
    mediator = DurableFakeMediator(public_guid, 2, ENTITLEMENT_STATUS_DISABLED, expired_at, 3)
    try:
        synchronized = await reconcile_entitlements_once(database.session, mediator)
        assert synchronized == 0

        async with database.session() as session:
            subscription = await session.get(Subscription, subscription_id)
            alert = (
                await session.execute(
                    select(NotificationOutbox).where(
                        NotificationOutbox.notification_kind == "operator_reconciliation_blocked"
                    )
                )
            ).scalar_one()
            assert subscription is not None
            assert subscription.reconciliation_state == "blocked"
            assert subscription.reconciliation_reason == "legacy_expiration_drift"
            assert alert.payload_json is not None
            assert '"reason_code": "legacy_expiration_drift"' in alert.payload_json
            assert '"suggested_action": "reconcile_adopt_expired"' in alert.payload_json
    finally:
        await database.dispose()


@pytest.mark.asyncio
async def test_vm010_notification_schema_separates_provider_acceptance_from_delivery(
    tmp_path: Path,
) -> None:
    database = Database(f"sqlite+aiosqlite:///{tmp_path / 'vm010.db'}")
    await database.initialize()
    try:
        async with database.session() as session:
            columns = await session.execute(select(func.count(NotificationOutbox.id)))
            assert columns.scalar_one() == 0
        # The migration itself is the contract: legacy notification rows are rebuilt with
        # provider_accepted_at_utc while delivered_at_utc remains nullable.
        async with database.engine.connect() as connection:
            result = await connection.exec_driver_sql("PRAGMA table_info(notification_deliveries)")
            names = {row[1] for row in result.fetchall()}
            assert {
                "claimed_at_utc",
                "send_started_at_utc",
                "provider_accepted_at_utc",
                "delivered_at_utc",
                "failed_at_utc",
            } <= names
    finally:
        await database.dispose()


@pytest.mark.asyncio
async def test_operation_state_machine_rejects_second_active_mutation_on_real_sqlite(
    tmp_path: Path,
) -> None:
    database = Database(f"sqlite+aiosqlite:///{tmp_path / 'operations.db'}")
    await database.initialize()
    expires_at = utc_now() + timedelta(days=10)
    user_id, subscription_id, _ = await _seed_subscription(database, expires_at=expires_at)
    try:
        async with database.session() as session:
            repository = EntitlementOperationRepository(session)
            first = await repository.create_once(
                user_id=user_id,
                subscription_id=subscription_id,
                operation_type="paid_activation",
                source_entity_type="order",
                source_entity_id="operation-one",
                idempotency_key="operation-one",
                duration_delta_seconds=86400,
                requested_device_limit=3,
                requested_status=ENTITLEMENT_STATUS_ACTIVE,
            )
            assert first.state == "pending"
            assert await repository.has_active_for_subscription(subscription_id)

        async with database.session() as session:
            count = await session.scalar(select(func.count(EntitlementOperation.id)))
            assert count == 1
            refund_count = await session.scalar(select(func.count(RefundOperation.id)))
            trial_count = await session.scalar(select(func.count(TrialClaim.id)))
            assert refund_count == 0
            assert trial_count == 0
    finally:
        await database.dispose()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "failpoint",
    [
        "before_local_prepare",
        "after_local_prepare",
        "before_request_send",
        "after_request_send",
        "after_remote_apply_before_response",
        "after_response_before_local_commit",
        "after_local_commit_before_notification",
        "after_notification_provider_acceptance",
    ],
)
async def test_entitlement_failure_windows_recover_without_double_apply(
    tmp_path: Path,
    failpoint: str,
) -> None:
    path = tmp_path / f"fault-{failpoint}.db"
    database = Database(f"sqlite+aiosqlite:///{path}")
    await database.initialize()
    expires_at = utc_now() + timedelta(days=7)
    user_id, subscription_id, public_guid = await _seed_subscription(
        database, expires_at=expires_at
    )
    mediator = DurableFakeMediator(public_guid, 1, ENTITLEMENT_STATUS_ACTIVE, expires_at, 3)
    operation_public_id: str | None = None

    async with database.session() as session:
        if failpoint == "before_local_prepare":
            inbox, _ = await PaymentInboxRepository(session).receive(
                provider="telegram_stars",
                provider_charge_id=f"charge-{failpoint}",
                invoice_payload=f"payload-{failpoint}",
                payer_external_id=str(user_id),
                amount_minor_units=199,
                currency="XTR",
            )
            await PaymentInboxRepository(session).mark_manual_review(
                inbox, "orchestration_interrupted_before_prepare"
            )
        else:
            repository = EntitlementOperationRepository(session)
            operation = await repository.create_once(
                user_id=user_id,
                subscription_id=subscription_id,
                operation_type="paid_activation",
                source_entity_type="fault_test",
                source_entity_id=failpoint,
                idempotency_key=f"fault:{failpoint}",
                duration_delta_seconds=3 * 86400,
                requested_device_limit=3,
                requested_status=ENTITLEMENT_STATUS_ACTIVE,
                observed_valid_until_utc=expires_at,
            )
            operation_public_id = operation.public_id
            if failpoint == "before_request_send":
                assert await repository.claim(operation, owner="dead-worker", lease_seconds=1)
                operation.claim_expires_at_utc = utc_now() - timedelta(seconds=1)
            elif failpoint in {
                "after_request_send",
                "after_remote_apply_before_response",
            }:
                await repository.mark_request_sent(operation)
            elif failpoint in {
                "after_response_before_local_commit",
                "after_local_commit_before_notification",
                "after_notification_provider_acceptance",
            }:
                result_until = expires_at + timedelta(days=3)
                result = MediatorEntitlementOperationResult(
                    status="applied",
                    operation_id=operation.public_id,
                    public_guid=public_guid,
                    operation_type="paid_activation",
                    expected_version=1,
                    result_version=2,
                    result_status=ENTITLEMENT_STATUS_ACTIVE,
                    result_valid_until_utc=result_until.isoformat(),
                    result_max_device_tokens=3,
                    applied_at_utc=utc_now().isoformat(),
                )
                mediator.operations[operation.public_id] = result
                mediator.version = 2
                mediator.valid_until_utc = result_until
                await repository.mark_external_applied(
                    operation,
                    result_version=2,
                    result_status=ENTITLEMENT_STATUS_ACTIVE,
                    result_valid_until_utc=result_until,
                    result_device_limit=3,
                )
                if failpoint in {
                    "after_local_commit_before_notification",
                    "after_notification_provider_acceptance",
                }:
                    subscription = await session.get(Subscription, subscription_id)
                    assert subscription is not None
                    subscription.expires_at = result_until
                    await EntitlementRepository(session).set_authoritative(
                        subscription,
                        version=2,
                        status=ENTITLEMENT_STATUS_ACTIVE,
                        valid_until_utc=result_until,
                        max_device_tokens=3,
                    )
                    await repository.mark_completed(operation)
                    outbox = await NotificationOutboxRepository(session).enqueue_once(
                        idempotency_key=f"fault-outbox:{failpoint}",
                        notification_kind="order_activated",
                        user_id=user_id,
                        subscription_id=subscription_id,
                    )
                    if failpoint == "after_notification_provider_acceptance":
                        await NotificationOutboxRepository(session).mark_provider_accepted(outbox)
            if failpoint == "after_remote_apply_before_response":
                result_until = expires_at + timedelta(days=3)
                mediator.version = 2
                mediator.valid_until_utc = result_until
                mediator.operations[operation.public_id] = MediatorEntitlementOperationResult(
                    status="applied",
                    operation_id=operation.public_id,
                    public_guid=public_guid,
                    operation_type="paid_activation",
                    expected_version=1,
                    result_version=2,
                    result_status=ENTITLEMENT_STATUS_ACTIVE,
                    result_valid_until_utc=result_until.isoformat(),
                    result_max_device_tokens=3,
                    applied_at_utc=utc_now().isoformat(),
                )
    await database.dispose()

    restarted = Database(f"sqlite+aiosqlite:///{path}")
    await restarted.initialize()
    try:
        if failpoint == "before_local_prepare":
            async with restarted.session() as session:
                assert await session.scalar(select(func.count(PaymentInbox.id))) == 1
                subscription = await session.get(Subscription, subscription_id)
                assert subscription is not None
                operation = await EntitlementOperationCoordinator(
                    session, mediator, worker_id="recovery"
                ).prepare_generic(
                    user_id=user_id,
                    subscription_id=subscription_id,
                    operation_type="paid_activation",
                    source_entity_type="fault_test",
                    source_entity_id=failpoint,
                    duration_delta_seconds=3 * 86400,
                    requested_device_limit=3,
                    requested_status=ENTITLEMENT_STATUS_ACTIVE,
                )
                operation_public_id = operation.public_id
        assert operation_public_id is not None

        async with restarted.session() as session:
            repository = EntitlementOperationRepository(session)
            operation = await repository.get_by_public_id(operation_public_id)
            subscription = await session.get(Subscription, subscription_id)
            assert operation is not None and subscription is not None
            if operation.state in {"claimed", "external_unknown"}:
                await EntitlementRecoveryWorker(session, mediator).classify(operation)
                await session.commit()
                operation = await repository.get_by_public_id(operation_public_id)
                assert operation is not None
            if operation.state != "completed":
                applied = await EntitlementOperationCoordinator(
                    session, mediator, worker_id="recovery"
                ).apply_generic(operation, subscription)
                assert applied is not None
                subscription.expires_at = applied.valid_until_utc
                subscription.max_devices = applied.max_device_tokens
                subscription.status = "active"
                await EntitlementRepository(session).set_authoritative(
                    subscription,
                    version=applied.version,
                    status=applied.status,
                    valid_until_utc=applied.valid_until_utc,
                    max_device_tokens=applied.max_device_tokens,
                )
                await repository.mark_completed(operation)
                await NotificationOutboxRepository(session).enqueue_once(
                    idempotency_key=f"fault-outbox:{failpoint}",
                    notification_kind="order_activated",
                    user_id=user_id,
                    subscription_id=subscription_id,
                )

        async with restarted.session() as session:
            operation = await EntitlementOperationRepository(session).get_by_public_id(
                operation_public_id
            )
            subscription = await session.get(Subscription, subscription_id)
            entitlement = await EntitlementRepository(session).get_for_subscription(subscription_id)
            assert operation is not None and operation.state == "completed"
            assert subscription is not None and entitlement is not None
            assert entitlement.version == mediator.version
            assert to_aware_utc(entitlement.valid_until_utc) == mediator.valid_until_utc
            assert subscription.status == "active"
            assert mediator.apply_calls <= 1
            outbox_count = await session.scalar(select(func.count(NotificationOutbox.id)))
            assert outbox_count == 1
            if failpoint == "after_notification_provider_acceptance":
                pending = await NotificationOutboxRepository(session).claim_batch()
                assert pending == []
    finally:
        await restarted.dispose()


@pytest.mark.asyncio
async def test_subscription_mutation_pairs_are_deterministically_serialized(
    tmp_path: Path,
) -> None:
    database = Database(f"sqlite+aiosqlite:///{tmp_path / 'mutation-concurrency.db'}")
    await database.initialize()
    expires_at = utc_now() + timedelta(days=7)
    user_id, _, _ = await _seed_subscription(database, expires_at=expires_at)
    pairs = [
        ("expiration", "renewal"),
        ("refund", "user_retry"),
        ("refund", "admin_retry"),
        ("trial", "paid_purchase"),
        ("referral", "old_order"),
        ("device_upgrade", "renewal"),
        ("reconciliation", "new_mutation"),
    ]

    async def compete(kind: str, key: str, ready: asyncio.Event, go: asyncio.Event) -> bool:
        async with database.session() as session:
            ready.set()
            await go.wait()
            return await AccessOperationLeaseRepository(session).acquire(
                user_id=user_id,
                owner_kind=kind,
                owner_key=key,
                lease_seconds=300,
            )

    try:
        for index, (left_kind, right_kind) in enumerate(pairs):
            left_ready = asyncio.Event()
            right_ready = asyncio.Event()
            go = asyncio.Event()
            left_key = f"{left_kind}:{index}"
            right_key = f"{right_kind}:{index}"
            left = asyncio.create_task(compete(left_kind, left_key, left_ready, go))
            right = asyncio.create_task(compete(right_kind, right_key, right_ready, go))
            await left_ready.wait()
            await right_ready.wait()
            go.set()
            results = await asyncio.gather(left, right)
            assert sum(results) == 1, (left_kind, right_kind, results)
            winner_key = left_key if results[0] else right_key
            async with database.session() as session:
                await AccessOperationLeaseRepository(session).release(
                    user_id=user_id, owner_key=winner_key
                )
    finally:
        await database.dispose()


async def _seed_legacy_activating_order(
    database: Database,
    *,
    user_id: int,
    subscription_id: int | None,
    base_version: int | None,
    base_valid_until: datetime | None,
    public_order_id: str,
) -> int:
    async with database.session() as session:
        order = Order(
            public_order_id=public_order_id,
            user_id=user_id,
            target_subscription_id=subscription_id,
            order_kind="extend" if subscription_id is not None else "purchase",
            base_entitlement_version=base_version,
            base_valid_until_utc=base_valid_until,
            requested_duration_days=30,
            purchased_duration_days=30,
            duration_days=30,
            requested_max_devices=3,
            selected_max_devices=3,
            status="activating",
            amount_minor_units=199,
            currency="XTR",
            provider="telegram_stars",
            provider_payment_id=f"charge-{public_order_id}",
            invoice_payload=f"payload-{public_order_id}",
            paid_at=utc_now(),
            created_at=utc_now(),
        )
        session.add(order)
        await session.flush()
        return order.id


@pytest.mark.asyncio
async def test_legacy_activating_order_retries_only_when_captured_base_is_unchanged(
    tmp_path: Path,
) -> None:
    database = Database(f"sqlite+aiosqlite:///{tmp_path / 'legacy-safe-retry.db'}")
    await database.initialize()
    expires_at = utc_now() + timedelta(days=10)
    user_id, subscription_id, public_guid = await _seed_subscription(
        database, expires_at=expires_at
    )
    order_id = await _seed_legacy_activating_order(
        database,
        user_id=user_id,
        subscription_id=subscription_id,
        base_version=1,
        base_valid_until=expires_at,
        public_order_id="legacy-safe-retry",
    )
    mediator = DurableFakeMediator(public_guid, 1, ENTITLEMENT_STATUS_ACTIVE, expires_at, 3)
    settings = Settings(
        TELEGRAM_BOT_TOKEN="test-token",
        ADMIN_TELEGRAM_IDS="1",
        MEDIATOR_ADMIN_TOKEN="test-admin-token",
    )
    try:
        recovered = await recover_legacy_activating_orders_once(
            database.session, mediator, settings
        )
        assert recovered == 1
        assert mediator.apply_calls == 1
        assert mediator.valid_until_utc == expires_at + timedelta(days=30)

        async with database.session() as session:
            order = await session.get(Order, order_id)
            application_count = await session.scalar(
                select(func.count(OrderApplication.id)).where(OrderApplication.order_id == order_id)
            )
            operation = (
                await session.execute(
                    select(EntitlementOperation).where(
                        EntitlementOperation.source_entity_type == "order",
                        EntitlementOperation.source_entity_id == "legacy-safe-retry",
                    )
                )
            ).scalar_one()
            assert order is not None and order.status == "paid"
            assert application_count == 1
            assert operation.state == "completed"
    finally:
        await database.dispose()


@pytest.mark.asyncio
async def test_legacy_activating_with_application_finalizes_without_remote_reapply(
    tmp_path: Path,
) -> None:
    database = Database(f"sqlite+aiosqlite:///{tmp_path / 'legacy-application.db'}")
    await database.initialize()
    expires_at = utc_now() + timedelta(days=10)
    user_id, subscription_id, public_guid = await _seed_subscription(
        database, expires_at=expires_at
    )
    order_id = await _seed_legacy_activating_order(
        database,
        user_id=user_id,
        subscription_id=subscription_id,
        base_version=1,
        base_valid_until=expires_at,
        public_order_id="legacy-with-application",
    )
    async with database.session() as session:
        session.add(
            OrderApplication(
                order_id=order_id,
                subscription_id=subscription_id,
                applied_at_utc=utc_now(),
                duration_days=30,
                selected_max_devices=3,
                resulting_valid_until_utc=expires_at,
                resulting_entitlement_version=1,
            )
        )
    mediator = DurableFakeMediator(public_guid, 1, ENTITLEMENT_STATUS_ACTIVE, expires_at, 3)
    settings = Settings(
        TELEGRAM_BOT_TOKEN="test-token",
        ADMIN_TELEGRAM_IDS="1",
        MEDIATOR_ADMIN_TOKEN="test-admin-token",
    )
    try:
        recovered = await recover_legacy_activating_orders_once(
            database.session, mediator, settings
        )
        assert recovered == 1
        assert mediator.apply_calls == 0
        async with database.session() as session:
            order = await session.get(Order, order_id)
            operation = (
                await session.execute(
                    select(EntitlementOperation).where(
                        EntitlementOperation.source_entity_id == "legacy-with-application"
                    )
                )
            ).scalar_one()
            assert order is not None and order.status == "paid"
            assert operation.state == "completed"
    finally:
        await database.dispose()


@pytest.mark.asyncio
async def test_legacy_activating_remote_newer_is_quarantined_without_retry(
    tmp_path: Path,
) -> None:
    database = Database(f"sqlite+aiosqlite:///{tmp_path / 'legacy-ambiguous.db'}")
    await database.initialize()
    expires_at = utc_now() + timedelta(days=10)
    user_id, subscription_id, public_guid = await _seed_subscription(
        database, expires_at=expires_at
    )
    order_id = await _seed_legacy_activating_order(
        database,
        user_id=user_id,
        subscription_id=subscription_id,
        base_version=1,
        base_valid_until=expires_at,
        public_order_id="legacy-ambiguous",
    )
    mediator = DurableFakeMediator(
        public_guid,
        2,
        ENTITLEMENT_STATUS_ACTIVE,
        expires_at + timedelta(days=30),
        3,
    )
    settings = Settings(
        TELEGRAM_BOT_TOKEN="test-token",
        ADMIN_TELEGRAM_IDS="1",
        MEDIATOR_ADMIN_TOKEN="test-admin-token",
    )
    try:
        recovered = await recover_legacy_activating_orders_once(
            database.session, mediator, settings
        )
        assert recovered == 0
        assert mediator.apply_calls == 0
        async with database.session() as session:
            order = await session.get(Order, order_id)
            subscription = await session.get(Subscription, subscription_id)
            operation = (
                await session.execute(
                    select(EntitlementOperation).where(
                        EntitlementOperation.source_entity_id == "legacy-ambiguous"
                    )
                )
            ).scalar_one()
            alert_count = await session.scalar(
                select(func.count(NotificationOutbox.id)).where(
                    NotificationOutbox.notification_kind == "operator_legacy_activation_quarantined"
                )
            )
            assert order is not None and order.status == "activating"
            assert subscription is not None
            assert subscription.reconciliation_state == "blocked"
            assert operation.state == "manual_review"
            assert operation.last_error_code == "legacy_activating_remote_state_ambiguous"
            assert alert_count == 1
    finally:
        await database.dispose()


@pytest.mark.asyncio
async def test_legacy_new_purchase_without_remote_identity_requires_manual_review(
    tmp_path: Path,
) -> None:
    database = Database(f"sqlite+aiosqlite:///{tmp_path / 'legacy-new-purchase.db'}")
    await database.initialize()
    async with database.session() as session:
        user = User(
            telegram_id=900099,
            username="legacy-new",
            first_name="Legacy",
            created_at=utc_now(),
            updated_at=utc_now(),
        )
        session.add(user)
        await session.flush()
        user_id = user.id
    order_id = await _seed_legacy_activating_order(
        database,
        user_id=user_id,
        subscription_id=None,
        base_version=None,
        base_valid_until=None,
        public_order_id="legacy-new-purchase",
    )
    # The Mediator object cannot be queried safely because the legacy create did not persist a
    # subscription public GUID. Recovery must not guess or create a second subscription.
    mediator = DurableFakeMediator(
        "00000000-0000-0000-0000-000000000999",
        1,
        ENTITLEMENT_STATUS_ACTIVE,
        utc_now() + timedelta(days=30),
        3,
    )
    settings = Settings(
        TELEGRAM_BOT_TOKEN="test-token",
        ADMIN_TELEGRAM_IDS="1",
        MEDIATOR_ADMIN_TOKEN="test-admin-token",
    )
    try:
        recovered = await recover_legacy_activating_orders_once(
            database.session, mediator, settings
        )
        assert recovered == 0
        assert mediator.apply_calls == 0
        async with database.session() as session:
            order = await session.get(Order, order_id)
            operation = (
                await session.execute(
                    select(EntitlementOperation).where(
                        EntitlementOperation.source_entity_id == "legacy-new-purchase"
                    )
                )
            ).scalar_one()
            assert order is not None and order.status == "activating"
            assert operation.state == "manual_review"
            assert operation.last_error_code == "legacy_new_purchase_remote_state_unknown"
    finally:
        await database.dispose()


@pytest.mark.asyncio
@pytest.mark.parametrize("replay_commands", list(product(["apply", "recover"], repeat=3)))
async def test_operation_state_machine_property_replays_apply_purchased_delta_at_most_once(
    tmp_path: Path,
    replay_commands: tuple[str, str, str],
) -> None:
    suffix = "-".join(replay_commands)
    database = Database(f"sqlite+aiosqlite:///{tmp_path / f'property-{suffix}.db'}")
    await database.initialize()
    expires_at = utc_now() + timedelta(days=7)
    user_id, subscription_id, public_guid = await _seed_subscription(
        database, expires_at=expires_at
    )
    mediator = DurableFakeMediator(public_guid, 1, ENTITLEMENT_STATUS_ACTIVE, expires_at, 3)
    operation_public_id: str
    try:
        async with database.session() as session:
            subscription = await session.get(Subscription, subscription_id)
            assert subscription is not None
            coordinator = EntitlementOperationCoordinator(session, mediator, worker_id="property")
            operation = await coordinator.prepare_generic(
                user_id=user_id,
                subscription_id=subscription_id,
                operation_type="paid_activation",
                source_entity_type="property_sequence",
                source_entity_id=suffix,
                duration_delta_seconds=30 * 86400,
                requested_device_limit=3,
                requested_status=ENTITLEMENT_STATUS_ACTIVE,
                observed_valid_until_utc=expires_at,
            )
            operation_public_id = operation.public_id
            await session.commit()
            first = await coordinator.apply_generic(operation, subscription)
            assert first is not None
            assert first.valid_until_utc == expires_at + timedelta(days=30)

        # Every command runs in a new session, modelling process object loss and replay after a
        # restart. All 2^3 command sequences must preserve the same remote business effect.
        for command in replay_commands:
            async with database.session() as session:
                operation = await EntitlementOperationRepository(session).get_by_public_id(
                    operation_public_id
                )
                subscription = await session.get(Subscription, subscription_id)
                assert operation is not None and subscription is not None
                if command == "apply":
                    operation.claim_expires_at_utc = utc_now() - timedelta(seconds=1)
                    await session.commit()
                    replay = await EntitlementOperationCoordinator(
                        session, mediator, worker_id=f"property-{command}"
                    ).apply_generic(operation, subscription)
                    assert replay is not None
                    assert replay.valid_until_utc == expires_at + timedelta(days=30)
                else:
                    state = await EntitlementRecoveryWorker(session, mediator).classify(operation)
                    assert state == "external_applied"
                    await session.commit()

        assert mediator.apply_calls == 1
        assert mediator.valid_until_utc == expires_at + timedelta(days=30)
    finally:
        await database.dispose()


@pytest.mark.asyncio
async def test_explicit_reconciliation_adopt_remote_is_audited_and_idempotent(
    tmp_path: Path,
) -> None:
    database = Database(f"sqlite+aiosqlite:///{tmp_path / 'repair-adopt.db'}")
    await database.initialize()
    local_until = utc_now() + timedelta(days=10)
    _, subscription_id, public_guid = await _seed_subscription(database, expires_at=local_until)
    remote_until = local_until + timedelta(days=5)
    mediator = DurableFakeMediator(public_guid, 2, ENTITLEMENT_STATUS_ACTIVE, remote_until, 4)
    try:
        async with database.session() as session:
            subscription = await session.get(Subscription, subscription_id)
            assert subscription is not None
            subscription.reconciliation_state = "blocked"
            subscription.reconciliation_reason = "remote_newer_unknown_origin"
            subscription.reconciliation_blocked_at_utc = utc_now()

        async with database.session() as session:
            service = ReconciliationRepairService(session, mediator)
            first = await service.apply(
                public_guid=public_guid,
                actor_telegram_id=1,
                source_request_id="repair-adopt-1",
                reason="verified_change_in_mediator_audit",
                mode="adopt_remote",
            )
            replay = await service.apply(
                public_guid=public_guid,
                actor_telegram_id=1,
                source_request_id="repair-adopt-2",
                reason="verified_change_in_mediator_audit",
                mode="adopt_remote",
            )
            assert first.operation_public_id == replay.operation_public_id
            assert first.version == 2
            assert first.subscription.expires_at == remote_until
            assert first.subscription.max_devices == 4
            assert first.subscription.reconciliation_state == "healthy"
            assert mediator.apply_calls == 0

            mediator.max_device_tokens = 5
            with pytest.raises(ValueError, match="reconciliation_snapshot_changed"):
                await service.apply(
                    public_guid=public_guid,
                    actor_telegram_id=1,
                    source_request_id="repair-adopt-3",
                    reason="same_version_payload_changed",
                    mode="adopt_remote",
                )

        async with database.session() as session:
            local = await EntitlementRepository(session).get_for_subscription(subscription_id)
            outbox_count = await session.scalar(
                select(func.count(NotificationOutbox.id)).where(
                    NotificationOutbox.notification_kind == "operator_reconciliation_repaired"
                )
            )
            assert local is not None
            assert local.version == 2
            assert to_aware_utc(local.valid_until_utc) == remote_until
            assert outbox_count == 1
    finally:
        await database.dispose()


@pytest.mark.asyncio
async def test_reconciliation_adopt_remote_rejects_ambiguous_disabled_entitlement(
    tmp_path: Path,
) -> None:
    database = Database(f"sqlite+aiosqlite:///{tmp_path / 'repair-ambiguous.db'}")
    await database.initialize()
    expired_at = utc_now() - timedelta(minutes=5)
    _, subscription_id, public_guid = await _seed_subscription(
        database,
        expires_at=expired_at,
        status=SUBSCRIPTION_STATUS_EXPIRED,
        entitlement_status=ENTITLEMENT_STATUS_ACTIVE,
    )
    mediator = DurableFakeMediator(public_guid, 2, ENTITLEMENT_STATUS_DISABLED, expired_at, 3)
    try:
        async with database.session() as session:
            subscription = await session.get(Subscription, subscription_id)
            assert subscription is not None
            subscription.reconciliation_state = "blocked"
            subscription.reconciliation_reason = "legacy_expiration_drift"
            subscription.reconciliation_blocked_at_utc = utc_now()

        async with database.session() as session:
            with pytest.raises(ValueError, match="ambiguous_disabled_entitlement_origin"):
                await ReconciliationRepairService(session, mediator).apply(
                    public_guid=public_guid,
                    actor_telegram_id=1,
                    source_request_id="repair-ambiguous-1",
                    reason="legacy_state_requires_explicit_intent",
                    mode="adopt_remote",
                )

        async with database.session() as session:
            subscription = await session.get(Subscription, subscription_id)
            local = await EntitlementRepository(session).get_for_subscription(subscription_id)
            assert subscription is not None and local is not None
            assert subscription.status == SUBSCRIPTION_STATUS_EXPIRED
            assert subscription.reconciliation_state == "blocked"
            assert local.version == 1
            assert local.status == ENTITLEMENT_STATUS_ACTIVE
            assert mediator.apply_calls == 0
    finally:
        await database.dispose()


@pytest.mark.asyncio
async def test_explicit_reconciliation_adopt_expired_preserves_business_lifecycle(
    tmp_path: Path,
) -> None:
    database = Database(f"sqlite+aiosqlite:///{tmp_path / 'repair-expired.db'}")
    await database.initialize()
    expired_at = utc_now() - timedelta(minutes=5)
    _, subscription_id, public_guid = await _seed_subscription(
        database,
        expires_at=expired_at,
        status=SUBSCRIPTION_STATUS_EXPIRED,
        entitlement_status=ENTITLEMENT_STATUS_ACTIVE,
    )
    mediator = DurableFakeMediator(public_guid, 2, ENTITLEMENT_STATUS_DISABLED, expired_at, 3)
    try:
        async with database.session() as session:
            subscription = await session.get(Subscription, subscription_id)
            assert subscription is not None
            subscription.reconciliation_state = "blocked"
            subscription.reconciliation_reason = "legacy_expiration_drift"
            subscription.reconciliation_blocked_at_utc = utc_now()

        async with database.session() as session:
            service = ReconciliationRepairService(session, mediator)
            first = await service.apply(
                public_guid=public_guid,
                actor_telegram_id=1,
                source_request_id="repair-expired-1",
                reason="confirmed_legacy_expiration",
                mode="adopt_expired",
                expected_remote_version=2,
            )
            replay = await service.apply(
                public_guid=public_guid,
                actor_telegram_id=1,
                source_request_id="repair-expired-2",
                reason="confirmed_legacy_expiration",
                mode="adopt_expired",
                expected_remote_version=2,
            )
            assert replay.operation_public_id == first.operation_public_id
            assert first.subscription.status == SUBSCRIPTION_STATUS_EXPIRED
            assert first.subscription.disabled_at is None
            assert first.subscription.reconciliation_state == "healthy"
            assert first.status == ENTITLEMENT_STATUS_DISABLED
            assert first.version == 2
            assert mediator.version == 2
            assert mediator.apply_calls == 0

        async with database.session() as session:
            local = await EntitlementRepository(session).get_for_subscription(subscription_id)
            outbox_count = await session.scalar(
                select(func.count(NotificationOutbox.id)).where(
                    NotificationOutbox.notification_kind == "operator_reconciliation_repaired"
                )
            )
            assert local is not None
            assert local.version == 2
            assert local.status == ENTITLEMENT_STATUS_DISABLED
            assert outbox_count == 1

            quote = await PurchaseService(
                session,
                Settings(
                    TELEGRAM_BOT_TOKEN="test-token",
                    PAYMENT_MODE="telegram_stars",
                    MEDIATOR_ADMIN_TOKEN="test-admin-token",
                    PRICING_BASE_DEVICE_MONTH_STARS=100,
                ),
                mediator,
            ).create_quote(
                telegram_id=900001,
                username="reliability-user",
                first_name="Reliability",
                period_count=1,
                max_devices=3,
                order_kind=ORDER_KIND_RESUME,
                target_subscription_id=subscription_id,
            )
            assert quote.order_kind == ORDER_KIND_RESUME
            assert quote.target_subscription_id == subscription_id
    finally:
        await database.dispose()


@pytest.mark.asyncio
async def test_reconciliation_adopt_expired_requires_exact_remote_version(
    tmp_path: Path,
) -> None:
    database = Database(f"sqlite+aiosqlite:///{tmp_path / 'repair-version.db'}")
    await database.initialize()
    expired_at = utc_now() - timedelta(minutes=5)
    _, subscription_id, public_guid = await _seed_subscription(
        database,
        expires_at=expired_at,
        status=SUBSCRIPTION_STATUS_EXPIRED,
        entitlement_status=ENTITLEMENT_STATUS_ACTIVE,
    )
    mediator = DurableFakeMediator(public_guid, 2, ENTITLEMENT_STATUS_DISABLED, expired_at, 3)
    try:
        async with database.session() as session:
            subscription = await session.get(Subscription, subscription_id)
            assert subscription is not None
            subscription.reconciliation_state = "blocked"
            subscription.reconciliation_reason = "legacy_expiration_drift"

        async with database.session() as session:
            with pytest.raises(ValueError, match="reconciliation_snapshot_changed"):
                await ReconciliationRepairService(session, mediator).apply(
                    public_guid=public_guid,
                    actor_telegram_id=1,
                    source_request_id="repair-version-1",
                    reason="stale_operator_snapshot",
                    mode="adopt_expired",
                    expected_remote_version=1,
                )
    finally:
        await database.dispose()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("validity_delta", "remote_devices", "expected_error"),
    [
        (timedelta(seconds=1), 3, "legacy_expiration_validity_mismatch"),
        (timedelta(0), 4, "legacy_expiration_device_limit_mismatch"),
    ],
)
async def test_reconciliation_adopt_expired_rejects_payload_mismatch(
    tmp_path: Path,
    validity_delta: timedelta,
    remote_devices: int,
    expected_error: str,
) -> None:
    database = Database(f"sqlite+aiosqlite:///{tmp_path / expected_error}.db")
    await database.initialize()
    expired_at = utc_now() - timedelta(minutes=5)
    _, subscription_id, public_guid = await _seed_subscription(
        database,
        expires_at=expired_at,
        status=SUBSCRIPTION_STATUS_EXPIRED,
        entitlement_status=ENTITLEMENT_STATUS_ACTIVE,
    )
    mediator = DurableFakeMediator(
        public_guid,
        2,
        ENTITLEMENT_STATUS_DISABLED,
        expired_at + validity_delta,
        remote_devices,
    )
    try:
        async with database.session() as session:
            subscription = await session.get(Subscription, subscription_id)
            assert subscription is not None
            subscription.reconciliation_state = "blocked"
            subscription.reconciliation_reason = "legacy_expiration_drift"

        async with database.session() as session:
            with pytest.raises(ValueError, match=expected_error):
                await ReconciliationRepairService(session, mediator).apply(
                    public_guid=public_guid,
                    actor_telegram_id=1,
                    source_request_id=f"repair-mismatch-{expected_error}",
                    reason="payload_must_match_exactly",
                    mode="adopt_expired",
                    expected_remote_version=2,
                )
            repair_count = await session.scalar(
                select(func.count(EntitlementOperation.id)).where(
                    EntitlementOperation.source_entity_type == "reconciliation_repair"
                )
            )
            assert repair_count == 0
    finally:
        await database.dispose()


@pytest.mark.asyncio
async def test_reconciliation_adopt_expired_rejects_active_entitlement_operation(
    tmp_path: Path,
) -> None:
    database = Database(f"sqlite+aiosqlite:///{tmp_path / 'repair-active-operation.db'}")
    await database.initialize()
    expired_at = utc_now() - timedelta(minutes=5)
    user_id, subscription_id, public_guid = await _seed_subscription(
        database,
        expires_at=expired_at,
        status=SUBSCRIPTION_STATUS_EXPIRED,
        entitlement_status=ENTITLEMENT_STATUS_ACTIVE,
    )
    mediator = DurableFakeMediator(public_guid, 2, ENTITLEMENT_STATUS_DISABLED, expired_at, 3)
    try:
        async with database.session() as session:
            subscription = await session.get(Subscription, subscription_id)
            assert subscription is not None
            subscription.reconciliation_state = "blocked"
            subscription.reconciliation_reason = "legacy_expiration_drift"
            await EntitlementOperationRepository(session).create_once(
                user_id=user_id,
                operation_type="paid_activation",
                source_entity_type="order",
                source_entity_id="unfinished-order",
                idempotency_key="entitlement:paid_activation:order:unfinished-order",
                subscription_id=subscription_id,
                duration_delta_seconds=30 * 86400,
                requested_device_limit=3,
                requested_status=ENTITLEMENT_STATUS_ACTIVE,
                observed_valid_until_utc=expired_at,
            )

        async with database.session() as session:
            with pytest.raises(ValueError, match="reconciliation_active_operation_exists"):
                await ReconciliationRepairService(session, mediator).apply(
                    public_guid=public_guid,
                    actor_telegram_id=1,
                    source_request_id="repair-active-operation",
                    reason="must_not_race_with_activation",
                    mode="adopt_expired",
                    expected_remote_version=2,
                )
    finally:
        await database.dispose()


@pytest.mark.asyncio
async def test_reconciliation_adopt_expired_detects_snapshot_change_before_commit(
    tmp_path: Path,
) -> None:
    database = Database(f"sqlite+aiosqlite:///{tmp_path / 'repair-race.db'}")
    await database.initialize()
    expired_at = utc_now() - timedelta(minutes=5)
    _, subscription_id, public_guid = await _seed_subscription(
        database,
        expires_at=expired_at,
        status=SUBSCRIPTION_STATUS_EXPIRED,
        entitlement_status=ENTITLEMENT_STATUS_ACTIVE,
    )
    mediator = ChangingSnapshotMediator(public_guid, 2, ENTITLEMENT_STATUS_DISABLED, expired_at, 3)
    try:
        async with database.session() as session:
            subscription = await session.get(Subscription, subscription_id)
            assert subscription is not None
            subscription.reconciliation_state = "blocked"
            subscription.reconciliation_reason = "legacy_expiration_drift"

        async with database.session() as session:
            with pytest.raises(ValueError, match="reconciliation_snapshot_changed"):
                await ReconciliationRepairService(session, mediator).apply(
                    public_guid=public_guid,
                    actor_telegram_id=1,
                    source_request_id="repair-race-1",
                    reason="operator_confirmed_snapshot_v2",
                    mode="adopt_expired",
                    expected_remote_version=2,
                )

        async with database.session() as session:
            subscription = await session.get(Subscription, subscription_id)
            local = await EntitlementRepository(session).get_for_subscription(subscription_id)
            operation = (
                await session.execute(
                    select(EntitlementOperation).where(
                        EntitlementOperation.source_entity_type == "reconciliation_repair",
                        EntitlementOperation.source_entity_id
                        == f"{subscription_id}:adopt_expired:remote-v2",
                    )
                )
            ).scalar_one()
            assert subscription is not None and local is not None
            assert subscription.status == SUBSCRIPTION_STATUS_EXPIRED
            assert subscription.reconciliation_state == "blocked"
            assert local.version == 1
            assert local.status == ENTITLEMENT_STATUS_ACTIVE
            assert operation.state == "manual_review"
            assert operation.last_error_code == "reconciliation_snapshot_changed"
            assert mediator.apply_calls == 0
    finally:
        await database.dispose()


@pytest.mark.asyncio
async def test_restore_local_rejects_active_entitlement_with_expired_validity(
    tmp_path: Path,
) -> None:
    database = Database(f"sqlite+aiosqlite:///{tmp_path / 'restore-expired-active.db'}")
    await database.initialize()
    expired_at = utc_now() - timedelta(minutes=5)
    _, subscription_id, public_guid = await _seed_subscription(database, expires_at=expired_at)
    mediator = DurableFakeMediator(public_guid, 2, ENTITLEMENT_STATUS_DISABLED, expired_at, 3)
    try:
        async with database.session() as session:
            subscription = await session.get(Subscription, subscription_id)
            assert subscription is not None
            subscription.reconciliation_state = "blocked"
            subscription.reconciliation_reason = "remote_newer_unknown_origin"

        async with database.session() as session:
            with pytest.raises(ValueError, match="active_entitlement_already_expired"):
                await ReconciliationRepairService(session, mediator).apply(
                    public_guid=public_guid,
                    actor_telegram_id=1,
                    source_request_id="restore-expired-active-1",
                    reason="unsafe_restore_must_be_rejected",
                    mode="restore_local",
                )
        assert mediator.apply_calls == 0
    finally:
        await database.dispose()


@pytest.mark.asyncio
async def test_explicit_reconciliation_restore_local_uses_durable_remote_operation_once(
    tmp_path: Path,
) -> None:
    database = Database(f"sqlite+aiosqlite:///{tmp_path / 'repair-restore.db'}")
    await database.initialize()
    local_until = utc_now() + timedelta(days=10)
    _, subscription_id, public_guid = await _seed_subscription(database, expires_at=local_until)
    mediator = DurableFakeMediator(
        public_guid,
        2,
        ENTITLEMENT_STATUS_ACTIVE,
        local_until + timedelta(days=20),
        4,
    )
    try:
        async with database.session() as session:
            subscription = await session.get(Subscription, subscription_id)
            assert subscription is not None
            subscription.reconciliation_state = "blocked"
            subscription.reconciliation_reason = "remote_newer_unknown_origin"
            subscription.reconciliation_blocked_at_utc = utc_now()

        async with database.session() as session:
            service = ReconciliationRepairService(session, mediator)
            first = await service.apply(
                public_guid=public_guid,
                actor_telegram_id=1,
                source_request_id="repair-restore-1",
                reason="remote_change_confirmed_invalid",
                mode="restore_local",
            )
            replay = await service.apply(
                public_guid=public_guid,
                actor_telegram_id=1,
                source_request_id="repair-restore-1",
                reason="remote_change_confirmed_invalid",
                mode="restore_local",
            )
            assert first.operation_public_id == replay.operation_public_id
            assert first.version == 3
            assert first.subscription.expires_at == local_until
            # The repair never reduces the authoritative device limit.
            assert first.subscription.max_devices == 4
            assert first.subscription.reconciliation_state == "healthy"
            assert mediator.apply_calls == 1
            assert mediator.valid_until_utc == local_until

        async with database.session() as session:
            operation = (
                await session.execute(
                    select(EntitlementOperation).where(
                        EntitlementOperation.source_entity_type == "reconciliation_repair",
                        EntitlementOperation.source_entity_id == "repair-restore-1",
                    )
                )
            ).scalar_one()
            assert operation.state == "completed"
            assert operation.external_result_version == 3
    finally:
        await database.dispose()


@pytest.mark.asyncio
async def test_explicit_reconciliation_adopt_disabled_preserves_forced_disable(
    tmp_path: Path,
) -> None:
    database = Database(f"sqlite+aiosqlite:///{tmp_path / 'repair-disabled.db'}")
    await database.initialize()
    valid_until = utc_now() + timedelta(days=5)
    _, subscription_id, public_guid = await _seed_subscription(
        database,
        expires_at=valid_until,
        status=ENTITLEMENT_STATUS_ACTIVE,
        entitlement_status=ENTITLEMENT_STATUS_ACTIVE,
    )
    mediator = DurableFakeMediator(public_guid, 2, ENTITLEMENT_STATUS_DISABLED, valid_until, 3)
    try:
        async with database.session() as session:
            subscription = await session.get(Subscription, subscription_id)
            assert subscription is not None
            subscription.reconciliation_state = "blocked"
            subscription.reconciliation_reason = "remote_newer_unknown_origin"
            subscription.reconciliation_blocked_at_utc = utc_now()
            await session.commit()

        async with database.session() as session:
            service = ReconciliationRepairService(session, mediator)
            outcome = await service.apply(
                public_guid=public_guid,
                actor_telegram_id=1,
                source_request_id="repair-disabled-1",
                reason="confirmed_admin_revoke",
                mode="adopt_disabled",
                expected_remote_version=2,
            )
            replay = await service.apply(
                public_guid=public_guid,
                actor_telegram_id=1,
                source_request_id="repair-disabled-2",
                reason="confirmed_admin_revoke",
                mode="adopt_disabled",
                expected_remote_version=2,
            )
            assert replay.operation_public_id == outcome.operation_public_id
            assert outcome.subscription.status == SUBSCRIPTION_STATUS_DISABLED
            assert outcome.subscription.disabled_at is not None
            assert outcome.status == ENTITLEMENT_STATUS_DISABLED
            assert outcome.version == 2
            assert mediator.apply_calls == 0

        async with database.session() as session:
            subscription = await session.get(Subscription, subscription_id)
            local = await EntitlementRepository(session).get_for_subscription(subscription_id)
            assert subscription is not None and local is not None
            assert subscription.status == SUBSCRIPTION_STATUS_DISABLED
            assert subscription.reconciliation_state == "healthy"
            assert local.status == ENTITLEMENT_STATUS_DISABLED
            assert local.version == 2
    finally:
        await database.dispose()


@pytest.mark.asyncio
async def test_reconciliation_adopt_disabled_rejects_non_disabled_remote_state(
    tmp_path: Path,
) -> None:
    database = Database(f"sqlite+aiosqlite:///{tmp_path / 'repair-disabled-wrong.db'}")
    await database.initialize()
    valid_until = utc_now() + timedelta(days=5)
    _, subscription_id, public_guid = await _seed_subscription(database, expires_at=valid_until)
    mediator = DurableFakeMediator(public_guid, 2, ENTITLEMENT_STATUS_ACTIVE, valid_until, 3)
    try:
        async with database.session() as session:
            subscription = await session.get(Subscription, subscription_id)
            assert subscription is not None
            subscription.reconciliation_state = "blocked"
            subscription.reconciliation_reason = "remote_newer_unknown_origin"
            subscription.reconciliation_blocked_at_utc = utc_now()
            await session.commit()

        async with database.session() as session:
            with pytest.raises(ValueError, match="reconciliation_remote_status_not_disabled"):
                await ReconciliationRepairService(session, mediator).apply(
                    public_guid=public_guid,
                    actor_telegram_id=1,
                    source_request_id="repair-disabled-wrong",
                    reason="operator_selected_wrong_action",
                    mode="adopt_disabled",
                    expected_remote_version=2,
                )
    finally:
        await database.dispose()
