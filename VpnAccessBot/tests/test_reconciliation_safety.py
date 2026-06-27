from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path

import pytest
from sqlalchemy import func, select, text

from vpn_access_bot.config import Settings
from vpn_access_bot.constants import (
    ENTITLEMENT_STATUS_ACTIVE,
    ORDER_KIND_EXTEND,
    ORDER_STATUS_ACTIVATION_FAILED,
    SUBSCRIPTION_STATUS_ACTIVE,
)
from vpn_access_bot.db import Database
from vpn_access_bot.mediator_client import MediatorEntitlementDetails
from vpn_access_bot.migrations import run_migrations
from vpn_access_bot.models import (
    AccessEntitlement,
    AuditEvent,
    EntitlementOperation,
    Subscription,
    User,
    utc_now,
)
from vpn_access_bot.operations import EntitlementOperationCoordinator
from vpn_access_bot.product_completion import reconcile_entitlements_once
from vpn_access_bot.repositories import EntitlementOperationRepository, OrderRepository
from vpn_access_bot.services import PurchaseService


@dataclass
class MappingMediator:
    entitlements: dict[str, MediatorEntitlementDetails]
    get_calls: list[str]

    async def get_entitlement(self, public_guid: str) -> MediatorEntitlementDetails:
        self.get_calls.append(public_guid)
        return self.entitlements[public_guid]


class NoMutationMediator:
    async def get_entitlement(self, public_guid: str):
        raise AssertionError(f"Unexpected Mediator call for {public_guid}")

    async def apply_entitlement_operation(self, *args, **kwargs):
        raise AssertionError("Blocked subscription must not reach Mediator mutation")


async def _seed_subscription(
    database: Database,
    *,
    telegram_id: int = 950001,
    public_guid: str = "00000000-0000-0000-0000-000000000951",
    reconciliation_state: str = "healthy",
    with_entitlement: bool = True,
    expires_at: datetime | None = None,
) -> tuple[int, int, str, datetime]:
    valid_until = expires_at or (utc_now() + timedelta(days=30))
    async with database.session() as session:
        user = User(
            telegram_id=telegram_id,
            username=f"user-{telegram_id}",
            first_name="Safety",
            created_at=utc_now(),
            updated_at=utc_now(),
        )
        session.add(user)
        await session.flush()
        subscription = Subscription(
            user_id=user.id,
            public_guid=public_guid,
            signed_url="",
            max_devices=3,
            status=SUBSCRIPTION_STATUS_ACTIVE,
            starts_at=valid_until - timedelta(days=30),
            expires_at=valid_until,
            created_at=valid_until - timedelta(days=30),
            updated_at_utc=utc_now(),
            reconciliation_state=reconciliation_state,
            reconciliation_reason=(
                "remote_newer_unknown_origin" if reconciliation_state != "healthy" else None
            ),
            reconciliation_blocked_at_utc=(
                utc_now() if reconciliation_state == "blocked" else None
            ),
        )
        session.add(subscription)
        await session.flush()
        user.primary_subscription_id = subscription.id
        if with_entitlement:
            session.add(
                AccessEntitlement(
                    subscription_id=subscription.id,
                    version=1,
                    status=ENTITLEMENT_STATUS_ACTIVE,
                    valid_until_utc=valid_until,
                    max_device_tokens=3,
                    updated_at_utc=utc_now(),
                )
            )
        await session.flush()
        return user.id, subscription.id, public_guid, valid_until


def _settings() -> Settings:
    return Settings(
        TELEGRAM_BOT_TOKEN="test-token",
        PAYMENT_MODE="telegram_stars",
        MEDIATOR_ADMIN_TOKEN="test-admin-token",
        PRICING_BASE_DEVICE_MONTH_STARS=100,
    )


async def _create_extend_order(
    database: Database,
    *,
    telegram_id: int,
    subscription_id: int,
):
    async with database.session() as session:
        service = PurchaseService(session, _settings(), NoMutationMediator())
        quote = await service.create_quote(
            telegram_id=telegram_id,
            username=f"user-{telegram_id}",
            first_name="Safety",
            period_count=1,
            max_devices=3,
            order_kind=ORDER_KIND_EXTEND,
            target_subscription_id=subscription_id,
        )
        order = await service.create_order_from_quote(
            quote.public_quote_id,
            actor_telegram_id=telegram_id,
        )
        return order.id, order.invoice_payload, order.amount_minor_units, order.currency


@pytest.mark.asyncio
async def test_blocked_subscription_cannot_create_quote(tmp_path: Path) -> None:
    database = Database(f"sqlite+aiosqlite:///{tmp_path / 'quote-block.db'}")
    await database.initialize()
    _, subscription_id, _, _ = await _seed_subscription(
        database,
        reconciliation_state="blocked",
    )
    try:
        async with database.session() as session:
            service = PurchaseService(session, _settings(), NoMutationMediator())
            with pytest.raises(ValueError, match="subscription_reconciliation_blocked"):
                await service.create_quote(
                    telegram_id=950001,
                    username="user-950001",
                    first_name="Safety",
                    period_count=1,
                    max_devices=3,
                    order_kind=ORDER_KIND_EXTEND,
                    target_subscription_id=subscription_id,
                )
    finally:
        await database.dispose()


@pytest.mark.asyncio
async def test_quote_is_rejected_if_reconciliation_blocks_before_order(tmp_path: Path) -> None:
    database = Database(f"sqlite+aiosqlite:///{tmp_path / 'order-block.db'}")
    await database.initialize()
    _, subscription_id, _, _ = await _seed_subscription(database)
    try:
        async with database.session() as session:
            service = PurchaseService(session, _settings(), NoMutationMediator())
            quote = await service.create_quote(
                telegram_id=950001,
                username="user-950001",
                first_name="Safety",
                period_count=1,
                max_devices=3,
                order_kind=ORDER_KIND_EXTEND,
                target_subscription_id=subscription_id,
            )
            subscription = await session.get(Subscription, subscription_id)
            assert subscription is not None
            subscription.reconciliation_state = "blocked"
            subscription.reconciliation_reason = "remote_newer_unknown_origin"
            with pytest.raises(ValueError, match="subscription_reconciliation_blocked"):
                await service.create_order_from_quote(
                    quote.public_quote_id,
                    actor_telegram_id=950001,
                )
            assert quote.consumed_at_utc is None
    finally:
        await database.dispose()


@pytest.mark.asyncio
async def test_precheckout_rejects_order_blocked_after_invoice_creation(tmp_path: Path) -> None:
    database = Database(f"sqlite+aiosqlite:///{tmp_path / 'precheckout-block.db'}")
    await database.initialize()
    _, subscription_id, _, _ = await _seed_subscription(database)
    try:
        order_id, payload, amount, currency = await _create_extend_order(
            database,
            telegram_id=950001,
            subscription_id=subscription_id,
        )
        async with database.session() as session:
            subscription = await session.get(Subscription, subscription_id)
            assert subscription is not None
            subscription.reconciliation_state = "blocked"
            subscription.reconciliation_reason = "remote_newer_unknown_origin"

        async with database.session() as session:
            valid, message = await PurchaseService(
                session,
                _settings(),
                NoMutationMediator(),
            ).validate_order_before_checkout(
                payload=payload,
                amount_minor_units=amount,
                currency=currency,
                payer_telegram_id=950001,
            )
            order = await OrderRepository(session).get_by_id(order_id)
            assert valid is False
            assert message is not None and "синхронизац" in message.lower()
            assert order is not None
            assert order.checkout_authorized_at_utc is None
    finally:
        await database.dispose()


@pytest.mark.asyncio
async def test_paid_order_blocked_before_activation_creates_no_operation(tmp_path: Path) -> None:
    database = Database(f"sqlite+aiosqlite:///{tmp_path / 'activation-block.db'}")
    await database.initialize()
    _, subscription_id, _, _ = await _seed_subscription(database)
    try:
        order_id, _, _, _ = await _create_extend_order(
            database,
            telegram_id=950001,
            subscription_id=subscription_id,
        )
        async with database.session() as session:
            order = await OrderRepository(session).get_by_id(order_id)
            subscription = await session.get(Subscription, subscription_id)
            assert order is not None and subscription is not None
            await OrderRepository(session).mark_payment_received(order, "charge-blocked")
            subscription.reconciliation_state = "blocked"
            subscription.reconciliation_reason = "remote_newer_unknown_origin"

        async with database.session() as session:
            order = await OrderRepository(session).get_by_id(order_id)
            assert order is not None
            outcome = await PurchaseService(
                session,
                _settings(),
                NoMutationMediator(),
            ).activate_order(order)
            operation_count = await session.scalar(select(func.count(EntitlementOperation.id)))
            refreshed = await OrderRepository(session).get_by_id(order_id)
            assert outcome.activated is False
            assert outcome.failure_code == "reconciliation_blocked"
            assert operation_count == 0
            assert refreshed is not None
            assert refreshed.status == ORDER_STATUS_ACTIVATION_FAILED
    finally:
        await database.dispose()


@pytest.mark.asyncio
async def test_reconciliation_backfills_missing_entitlement_from_remote(tmp_path: Path) -> None:
    database = Database(f"sqlite+aiosqlite:///{tmp_path / 'backfill.db'}")
    await database.initialize()
    _, subscription_id, public_guid, valid_until = await _seed_subscription(
        database,
        with_entitlement=False,
    )
    mediator = MappingMediator(
        entitlements={
            public_guid: MediatorEntitlementDetails(
                public_guid=public_guid,
                version=7,
                status=ENTITLEMENT_STATUS_ACTIVE,
                valid_until_utc=valid_until.isoformat(),
                max_device_tokens=3,
                updated_at_utc=utc_now().isoformat(),
            )
        },
        get_calls=[],
    )
    try:
        synchronized = await reconcile_entitlements_once(database.session, mediator)
        assert synchronized == 1
        assert mediator.get_calls == [public_guid]
        async with database.session() as session:
            entitlement = (
                await session.execute(
                    select(AccessEntitlement).where(
                        AccessEntitlement.subscription_id == subscription_id
                    )
                )
            ).scalar_one()
            subscription = await session.get(Subscription, subscription_id)
            assert entitlement.version == 7
            assert entitlement.status == ENTITLEMENT_STATUS_ACTIVE
            assert subscription is not None
            assert subscription.reconciliation_state == "healthy"
    finally:
        await database.dispose()


@pytest.mark.asyncio
async def test_one_malformed_remote_snapshot_does_not_abort_other_subscriptions(
    tmp_path: Path,
) -> None:
    database = Database(f"sqlite+aiosqlite:///{tmp_path / 'malformed-isolation.db'}")
    await database.initialize()
    _, first_id, first_guid, first_until = await _seed_subscription(
        database,
        telegram_id=950001,
        public_guid="00000000-0000-0000-0000-000000000951",
    )
    _, second_id, second_guid, second_until = await _seed_subscription(
        database,
        telegram_id=950002,
        public_guid="00000000-0000-0000-0000-000000000952",
    )
    mediator = MappingMediator(
        entitlements={
            first_guid: MediatorEntitlementDetails(
                public_guid=first_guid,
                version=1,
                status=ENTITLEMENT_STATUS_ACTIVE,
                valid_until_utc="not-a-timestamp",
                max_device_tokens=3,
                updated_at_utc=utc_now().isoformat(),
            ),
            second_guid: MediatorEntitlementDetails(
                public_guid=second_guid,
                version=1,
                status=ENTITLEMENT_STATUS_ACTIVE,
                valid_until_utc=second_until.isoformat(),
                max_device_tokens=3,
                updated_at_utc=utc_now().isoformat(),
            ),
        },
        get_calls=[],
    )
    try:
        synchronized = await reconcile_entitlements_once(database.session, mediator)
        assert synchronized == 1
        assert mediator.get_calls == [first_guid, second_guid]
        async with database.session() as session:
            first = await session.get(Subscription, first_id)
            second = await session.get(Subscription, second_id)
            failure_count = await session.scalar(
                select(func.count(AuditEvent.id)).where(
                    AuditEvent.subscription_id == first_id,
                    AuditEvent.event_type == "entitlement.reconciliation_failed",
                )
            )
            assert first is not None and second is not None
            assert first.reconciliation_state == "blocked"
            assert first.reconciliation_reason == "invalid_remote_entitlement"
            assert second.reconciliation_state == "healthy"
            assert failure_count == 1
            assert first_until != second_until or first_id != second_id
    finally:
        await database.dispose()


@pytest.mark.asyncio
async def test_migration_25_quarantines_only_unsent_blocked_order_operation(
    tmp_path: Path,
) -> None:
    database = Database(f"sqlite+aiosqlite:///{tmp_path / 'migration-25.db'}")
    await database.initialize()
    _, subscription_id, _, _ = await _seed_subscription(database)
    try:
        order_id, _, _, _ = await _create_extend_order(
            database,
            telegram_id=950001,
            subscription_id=subscription_id,
        )
        async with database.session() as session:
            order = await OrderRepository(session).get_by_id(order_id)
            subscription = await session.get(Subscription, subscription_id)
            assert order is not None and subscription is not None
            await OrderRepository(session).mark_payment_received(order, "migration-25-charge")
            await OrderRepository(session).mark_activation_failed(
                order,
                "reconciliation_blocked",
            )
            subscription.reconciliation_state = "blocked"
            subscription.reconciliation_reason = "remote_newer_unknown_origin"
            operation = await EntitlementOperationRepository(session).create_once(
                user_id=order.user_id,
                operation_type="paid_activation",
                source_entity_type="order",
                source_entity_id=order.public_order_id,
                idempotency_key=f"entitlement:order:{order.public_order_id}",
                subscription_id=subscription_id,
                duration_delta_seconds=30 * 86400,
                requested_device_limit=3,
                requested_status=ENTITLEMENT_STATUS_ACTIVE,
                observed_valid_until_utc=order.base_valid_until_utc,
            )
            assert operation.state == "pending"
            await session.execute(text("DELETE FROM schema_migrations WHERE version = 25"))

        async with database.engine.begin() as connection:
            await run_migrations(connection)
            await run_migrations(connection)

        async with database.session() as session:
            operation = (await session.execute(select(EntitlementOperation))).scalar_one()
            migration_count = await session.scalar(
                select(func.count())
                .select_from(text("schema_migrations"))
                .where(text("version = 25"))
            )
            assert operation.state == "manual_review"
            assert operation.last_error_code == "reconciliation_blocked"
            assert operation.external_request_sent_at_utc is None
            assert migration_count == 1
    finally:
        await database.dispose()


@pytest.mark.asyncio
async def test_safe_manual_review_operation_is_rearmed_after_reconciliation_repair(
    tmp_path: Path,
) -> None:
    database = Database(f"sqlite+aiosqlite:///{tmp_path / 'rearm.db'}")
    await database.initialize()
    _, subscription_id, _, _ = await _seed_subscription(database)
    try:
        order_id, _, _, _ = await _create_extend_order(
            database,
            telegram_id=950001,
            subscription_id=subscription_id,
        )
        async with database.session() as session:
            order = await OrderRepository(session).get_by_id(order_id)
            assert order is not None
            repository = EntitlementOperationRepository(session)
            operation = await repository.create_once(
                user_id=order.user_id,
                operation_type="paid_activation",
                source_entity_type="order",
                source_entity_id=order.public_order_id,
                idempotency_key=f"entitlement:order:{order.public_order_id}",
                subscription_id=subscription_id,
                duration_delta_seconds=30 * 86400,
                requested_device_limit=3,
                requested_status=ENTITLEMENT_STATUS_ACTIVE,
                observed_valid_until_utc=order.base_valid_until_utc,
            )
            await repository.mark_manual_review(operation, "reconciliation_blocked")

        async with database.session() as session:
            order = await OrderRepository(session).get_by_id(order_id)
            assert order is not None
            operation = await EntitlementOperationCoordinator(
                session,
                NoMutationMediator(),
            ).prepare_order(order)
            assert operation.state == "pending"
            assert operation.last_error_code is None
    finally:
        await database.dispose()


@pytest.mark.asyncio
async def test_missing_entitlement_with_remote_disabled_is_backfilled_and_blocked(
    tmp_path: Path,
) -> None:
    database = Database(f"sqlite+aiosqlite:///{tmp_path / 'backfill-disabled.db'}")
    await database.initialize()
    _, subscription_id, public_guid, valid_until = await _seed_subscription(
        database,
        with_entitlement=False,
    )
    mediator = MappingMediator(
        entitlements={
            public_guid: MediatorEntitlementDetails(
                public_guid=public_guid,
                version=4,
                status="disabled",
                valid_until_utc=valid_until.isoformat(),
                max_device_tokens=3,
                updated_at_utc=utc_now().isoformat(),
            )
        },
        get_calls=[],
    )
    try:
        synchronized = await reconcile_entitlements_once(database.session, mediator)
        assert synchronized == 0
        async with database.session() as session:
            entitlement = (
                await session.execute(
                    select(AccessEntitlement).where(
                        AccessEntitlement.subscription_id == subscription_id
                    )
                )
            ).scalar_one()
            subscription = await session.get(Subscription, subscription_id)
            assert entitlement.version == 4
            assert entitlement.status == "disabled"
            assert subscription is not None
            assert subscription.reconciliation_state == "blocked"
            assert subscription.reconciliation_reason == "lifecycle_payload_mismatch"
    finally:
        await database.dispose()
