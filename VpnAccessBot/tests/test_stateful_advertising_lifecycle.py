from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path

import pytest
from sqlalchemy import func, select

from vpn_access_bot.config import Settings
from vpn_access_bot.constants import ENTITLEMENT_STATUS_ACTIVE
from vpn_access_bot.db import Database
from vpn_access_bot.mediator_client import (
    MediatorClientError,
    MediatorEntitlementDetails,
    MediatorEntitlementOperationResult,
)
from vpn_access_bot.models import (
    AccessEntitlement,
    CommercialEntitlementAdjustment,
    CommercialEntitlementSegment,
    EntitlementOperation,
    Order,
    OrderApplication,
    RefundOperation,
    RefundPlan,
    Subscription,
    User,
    utc_now,
)
from vpn_access_bot.product_completion import recover_refund_operations_once
from vpn_access_bot.repositories import to_aware_utc
from vpn_access_bot.services import PurchaseService


def _settings() -> Settings:
    return Settings(
        TELEGRAM_BOT_TOKEN="test-token",
        ADMIN_TELEGRAM_IDS="1",
        MEDIATOR_ADMIN_TOKEN="test-admin-token",
    )


@dataclass
class _RemoteEntitlement:
    version: int
    status: str
    valid_until_utc: datetime
    max_device_tokens: int


@dataclass
class StatefulMediator:
    entitlements: dict[str, _RemoteEntitlement] = field(default_factory=dict)
    operations: dict[str, MediatorEntitlementOperationResult] = field(default_factory=dict)
    apply_count_by_operation: dict[str, int] = field(default_factory=dict)
    hide_result_once_for_guid: set[str] = field(default_factory=set)
    hidden_operations: set[str] = field(default_factory=set)

    async def get_entitlement(self, public_guid: str) -> MediatorEntitlementDetails:
        remote = self.entitlements[public_guid]
        return MediatorEntitlementDetails(
            public_guid=public_guid,
            version=remote.version,
            status=remote.status,
            valid_until_utc=remote.valid_until_utc.isoformat(),
            max_device_tokens=remote.max_device_tokens,
            updated_at_utc=utc_now().isoformat(),
        )

    async def get_entitlement_operation(
        self, operation_id: str
    ) -> MediatorEntitlementOperationResult | None:
        if operation_id in self.hidden_operations:
            return None
        return self.operations.get(operation_id)

    async def get_entitlement_operation_by_result_version(
        self, public_guid: str, result_version: int
    ) -> MediatorEntitlementOperationResult | None:
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
        existing = self.operations.get(operation_id)
        if existing is not None:
            return existing
        remote = self.entitlements[public_guid]
        if expected_version != remote.version:
            raise MediatorClientError(
                "version conflict",
                error_code="entitlement_operation_version_conflict",
            )
        self.apply_count_by_operation[operation_id] = (
            self.apply_count_by_operation.get(operation_id, 0) + 1
        )
        remote.version += 1
        remote.status = status
        remote.valid_until_utc = to_aware_utc(datetime.fromisoformat(valid_until_utc))
        remote.max_device_tokens = max_device_tokens
        result = MediatorEntitlementOperationResult(
            status="applied",
            operation_id=operation_id,
            public_guid=public_guid,
            operation_type=operation_type,
            expected_version=expected_version,
            result_version=remote.version,
            result_status=status,
            result_valid_until_utc=remote.valid_until_utc.isoformat(),
            result_max_device_tokens=max_device_tokens,
            applied_at_utc=utc_now().isoformat(),
        )
        self.operations[operation_id] = result
        if public_guid in self.hide_result_once_for_guid:
            self.hide_result_once_for_guid.remove(public_guid)
            self.hidden_operations.add(operation_id)
            raise MediatorClientError(
                "response lost after remote commit",
                error_code="mediator_outcome_unknown",
            )
        return result

    def reveal_remote_results(self) -> None:
        self.hidden_operations.clear()


@dataclass
class FakeRefundProvider:
    refunded_charge_ids: set[str] = field(default_factory=set)
    call_count_by_charge: dict[str, int] = field(default_factory=dict)

    async def refund(self, charge_id: str) -> None:
        self.call_count_by_charge[charge_id] = self.call_count_by_charge.get(charge_id, 0) + 1
        self.refunded_charge_ids.add(charge_id)


async def _seed_orders(
    database: Database,
    mediator: StatefulMediator,
    *,
    count: int,
) -> list[tuple[int, str, datetime, int]]:
    seeded: list[tuple[int, str, datetime, int]] = []
    order_kinds = ("extend", "upgrade_devices", "extend_and_upgrade")
    async with database.session() as session:
        for index in range(count):
            order_kind = order_kinds[index % len(order_kinds)]
            previous_valid_until = utc_now() + timedelta(days=20 + index)
            days_added = 0 if order_kind == "upgrade_devices" else 30
            previous_devices = 3
            current_devices = 5 if order_kind != "extend" else 3
            current_valid_until = previous_valid_until + timedelta(days=days_added)
            user = User(
                telegram_id=880000 + index,
                username=f"load-user-{index}",
                first_name="Load",
                created_at=utc_now(),
                updated_at=utc_now(),
            )
            session.add(user)
            await session.flush()
            public_guid = f"20000000-0000-0000-0000-{index:012d}"
            subscription = Subscription(
                user_id=user.id,
                public_guid=public_guid,
                signed_url="",
                max_devices=current_devices,
                status="active",
                starts_at=previous_valid_until - timedelta(days=30),
                expires_at=current_valid_until,
                created_at=utc_now(),
                updated_at_utc=utc_now(),
            )
            session.add(subscription)
            await session.flush()
            user.primary_subscription_id = subscription.id
            session.add(
                AccessEntitlement(
                    subscription_id=subscription.id,
                    version=2,
                    status=ENTITLEMENT_STATUS_ACTIVE,
                    valid_until_utc=current_valid_until,
                    max_device_tokens=current_devices,
                    updated_at_utc=utc_now(),
                )
            )
            order = Order(
                public_order_id=f"load-refund-{index}",
                user_id=user.id,
                target_subscription_id=subscription.id,
                order_kind=order_kind,
                status="paid",
                duration_days=days_added,
                selected_max_devices=current_devices,
                base_entitlement_version=1,
                base_valid_until_utc=previous_valid_until,
                base_max_devices=previous_devices,
                amount_minor_units=299,
                final_amount_minor_units=299,
                currency="XTR",
                provider="telegram_stars",
                provider_payment_id=f"load-charge-{index}",
                invoice_payload=f"load-payload-{index}",
                paid_at=utc_now(),
                completed_at_utc=utc_now(),
                created_at=utc_now(),
            )
            session.add(order)
            await session.flush()
            session.add(
                OrderApplication(
                    order_id=order.id,
                    subscription_id=subscription.id,
                    applied_at_utc=utc_now(),
                    duration_days=days_added,
                    selected_max_devices=current_devices,
                    resulting_valid_until_utc=current_valid_until,
                    resulting_entitlement_version=2,
                    previous_entitlement_version=1,
                    previous_status="active",
                    previous_valid_until_utc=previous_valid_until,
                    previous_max_devices=previous_devices,
                )
            )
            session.add(
                CommercialEntitlementAdjustment(
                    subscription_id=subscription.id,
                    source_kind="paid_order",
                    duration_delta_seconds=days_added * 86400,
                    device_limit_before=previous_devices,
                    device_limit_after=current_devices,
                    source_order_id=order.id,
                    idempotency_key=f"order:{order.public_order_id}",
                    status="applied",
                    created_at_utc=utc_now(),
                    applied_at_utc=utc_now(),
                )
            )
            if days_added:
                session.add(
                    CommercialEntitlementSegment(
                        subscription_id=subscription.id,
                        source_kind="paid_order",
                        starts_at_utc=previous_valid_until,
                        ends_at_utc=current_valid_until,
                        source_order_id=order.id,
                        source_entity_id=str(order.id),
                        idempotency_key=f"order:{order.public_order_id}",
                        status="applied",
                        created_at_utc=utc_now(),
                    )
                )
            mediator.entitlements[public_guid] = _RemoteEntitlement(
                version=2,
                status="active",
                valid_until_utc=current_valid_until,
                max_device_tokens=current_devices,
            )
            if index % 3 == 0:
                mediator.hide_result_once_for_guid.add(public_guid)
            seeded.append((order.id, public_guid, previous_valid_until, previous_devices))
        await session.commit()
    return seeded


@pytest.mark.asyncio
async def test_stateful_concurrent_refund_load_recovers_unknown_remote_results_after_restart(
    tmp_path: Path,
) -> None:
    database_url = f"sqlite+aiosqlite:///{tmp_path / 'stateful-lifecycle.db'}"
    database = Database(database_url)
    await database.initialize()
    settings = _settings()
    mediator = StatefulMediator()
    provider = FakeRefundProvider()
    seeded = await _seed_orders(database, mediator, count=12)

    async def execute(order_id: int) -> None:
        async with database.session() as session:
            service = PurchaseService(session, settings, mediator)
            preview = await service.preview_refund(order_id, admin_telegram_id=1)
            assert preview.is_eligible
            assert preview.confirmation_token is not None
            candidate = await service.confirm_refund(
                preview.confirmation_token,
                admin_telegram_id=1,
            )
        assert candidate.charge_id is not None
        await provider.refund(candidate.charge_id)
        async with database.session() as session:
            try:
                await PurchaseService(session, settings, mediator).complete_refund_after_provider(
                    order_id
                )
            except MediatorClientError as error:
                assert error.error_code == "mediator_outcome_unknown"

    await asyncio.gather(*(execute(order_id) for order_id, *_ in seeded))

    # Simulate a full process restart: all local sessions are closed and a new Database object
    # recovers durable operations against the provider state that already committed remotely.
    await database.dispose()
    mediator.reveal_remote_results()
    restarted = Database(database_url)
    await restarted.initialize()
    await recover_refund_operations_once(restarted.session, mediator, settings)
    await recover_refund_operations_once(restarted.session, mediator, settings)

    try:
        async with restarted.session() as session:
            refunded_orders = await session.scalar(
                select(func.count(Order.id)).where(Order.status == "refunded")
            )
            completed_refunds = await session.scalar(
                select(func.count(RefundOperation.id)).where(RefundOperation.state == "completed")
            )
            completed_plans = await session.scalar(
                select(func.count(RefundPlan.id)).where(RefundPlan.state == "completed")
            )
            pending_entitlements = await session.scalar(
                select(func.count(EntitlementOperation.id)).where(
                    EntitlementOperation.state != "completed"
                )
            )
            assert refunded_orders == len(seeded)
            assert completed_refunds == len(seeded)
            assert completed_plans == len(seeded)
            assert pending_entitlements == 0

            for order_id, public_guid, previous_valid_until, previous_devices in seeded:
                order = await session.get(Order, order_id)
                assert order is not None
                subscription = await session.get(Subscription, order.target_subscription_id)
                assert subscription is not None
                assert subscription.max_devices == previous_devices
                assert (
                    abs(
                        (
                            to_aware_utc(subscription.expires_at)
                            - to_aware_utc(previous_valid_until)
                        ).total_seconds()
                    )
                    < 1
                )
                operation = (
                    await session.execute(
                        select(EntitlementOperation).where(
                            EntitlementOperation.source_entity_type == "refund",
                            EntitlementOperation.subscription_id == subscription.id,
                        )
                    )
                ).scalar_one()
                assert mediator.apply_count_by_operation[operation.public_id] == 1
                assert mediator.entitlements[public_guid].version == 3

        assert len(provider.refunded_charge_ids) == len(seeded)
        assert all(value == 1 for value in provider.call_count_by_charge.values())
    finally:
        await restarted.dispose()
