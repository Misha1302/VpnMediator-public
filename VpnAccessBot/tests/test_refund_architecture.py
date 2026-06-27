from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path

import pytest
from sqlalchemy import select

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
    Order,
    OrderApplication,
    ReferralReward,
    RefundOperation,
    RefundPlan,
    Subscription,
    User,
    utc_now,
)
from vpn_access_bot.product_completion import process_referral_rewards_once
from vpn_access_bot.repositories import to_aware_utc
from vpn_access_bot.services import PurchaseService


def _settings() -> Settings:
    return Settings(
        TELEGRAM_BOT_TOKEN="test-token",
        ADMIN_TELEGRAM_IDS="1",
        MEDIATOR_ADMIN_TOKEN="test-admin-token",
    )


@dataclass
class RefundMediator:
    public_guid: str
    version: int
    status: str
    valid_until_utc: datetime
    max_device_tokens: int
    operations: dict[str, MediatorEntitlementOperationResult] = field(default_factory=dict)
    apply_calls: int = 0

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
            (item for item in self.operations.values() if item.result_version == result_version),
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


async def _seed_applied_order(
    database: Database,
    *,
    order_kind: str,
    previous_valid_until: datetime,
    current_valid_until: datetime,
    previous_max_devices: int,
    current_max_devices: int,
    include_before_snapshot: bool = True,
) -> tuple[int, int, str]:
    async with database.session() as session:
        user = User(
            telegram_id=700001,
            username="refund-user",
            first_name="Refund",
            created_at=utc_now(),
            updated_at=utc_now(),
        )
        session.add(user)
        await session.flush()
        public_guid = "00000000-0000-0000-0000-000000000777"
        subscription = Subscription(
            user_id=user.id,
            public_guid=public_guid,
            signed_url="",
            max_devices=current_max_devices,
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
                max_device_tokens=current_max_devices,
                updated_at_utc=utc_now(),
            )
        )
        order = Order(
            public_order_id=f"refund-{order_kind}",
            user_id=user.id,
            target_subscription_id=subscription.id,
            order_kind=order_kind,
            status="paid",
            duration_days=max((current_valid_until - previous_valid_until).days, 0),
            selected_max_devices=current_max_devices,
            base_entitlement_version=1,
            base_valid_until_utc=previous_valid_until,
            base_max_devices=previous_max_devices,
            amount_minor_units=299,
            final_amount_minor_units=299,
            currency="XTR",
            provider="telegram_stars",
            provider_payment_id=f"charge-{order_kind}",
            invoice_payload=f"payload-{order_kind}",
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
                duration_days=order.duration_days,
                selected_max_devices=current_max_devices,
                resulting_valid_until_utc=current_valid_until,
                resulting_entitlement_version=2,
                previous_entitlement_version=(1 if include_before_snapshot else None),
                previous_status=("active" if include_before_snapshot else None),
                previous_valid_until_utc=(
                    previous_valid_until if include_before_snapshot else None
                ),
                previous_max_devices=(previous_max_devices if include_before_snapshot else None),
            )
        )
        session.add(
            CommercialEntitlementAdjustment(
                subscription_id=subscription.id,
                source_kind="paid_order",
                duration_delta_seconds=max(
                    int((current_valid_until - previous_valid_until).total_seconds()), 0
                ),
                device_limit_before=previous_max_devices,
                device_limit_after=current_max_devices,
                source_order_id=order.id,
                idempotency_key=f"order:{order.public_order_id}",
                status="applied",
                created_at_utc=utc_now(),
                applied_at_utc=utc_now(),
            )
        )
        if current_valid_until > previous_valid_until:
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
        await session.flush()
        return order.id, subscription.id, public_guid


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("order_kind", "days_added", "old_devices", "new_devices"),
    [
        ("extend", 30, 3, 3),
        ("upgrade_devices", 0, 3, 5),
        ("extend_and_upgrade", 30, 3, 5),
    ],
)
async def test_refund_restores_only_the_orders_entitlement_contribution(
    tmp_path: Path,
    order_kind: str,
    days_added: int,
    old_devices: int,
    new_devices: int,
) -> None:
    database = Database(f"sqlite+aiosqlite:///{tmp_path / f'{order_kind}.db'}")
    await database.initialize()
    previous_valid_until = utc_now() + timedelta(days=20)
    current_valid_until = previous_valid_until + timedelta(days=days_added)
    order_id, subscription_id, public_guid = await _seed_applied_order(
        database,
        order_kind=order_kind,
        previous_valid_until=previous_valid_until,
        current_valid_until=current_valid_until,
        previous_max_devices=old_devices,
        current_max_devices=new_devices,
    )
    mediator = RefundMediator(
        public_guid=public_guid,
        version=2,
        status="active",
        valid_until_utc=current_valid_until,
        max_device_tokens=new_devices,
    )
    try:
        async with database.session() as session:
            service = PurchaseService(session, _settings(), mediator)
            preview = await service.preview_refund(order_id, admin_telegram_id=1)
            assert preview.is_eligible is True
            assert preview.confirmation_token is not None
            assert to_aware_utc(preview.target_valid_until_utc) == to_aware_utc(
                previous_valid_until
            )
            assert preview.target_max_devices == old_devices
            await service.confirm_refund(
                preview.confirmation_token,
                admin_telegram_id=1,
            )
            with pytest.raises(ValueError, match="refund_confirmation_invalid"):
                await service.confirm_refund(
                    preview.confirmation_token,
                    admin_telegram_id=1,
                )
            await service.complete_refund_after_provider(order_id)

        async with database.session() as session:
            subscription = await session.get(Subscription, subscription_id)
            order = await session.get(Order, order_id)
            assert subscription is not None
            assert order is not None
            assert order.status == "refunded"
            assert subscription.status == "active"
            assert to_aware_utc(subscription.expires_at) == to_aware_utc(previous_valid_until)
            assert subscription.max_devices == old_devices
            adjustment = await session.scalar(
                select(CommercialEntitlementAdjustment).where(
                    CommercialEntitlementAdjustment.source_order_id == order_id
                )
            )
            assert adjustment is not None
            assert adjustment.status == "reversed"
            plan = await session.scalar(select(RefundPlan).where(RefundPlan.order_id == order_id))
            assert plan is not None
            assert plan.state == "completed"
        assert mediator.apply_calls == 1
    finally:
        await database.dispose()


@pytest.mark.asyncio
async def test_legacy_order_without_complete_snapshot_is_rejected_before_provider_call(
    tmp_path: Path,
) -> None:
    database = Database(f"sqlite+aiosqlite:///{tmp_path / 'legacy.db'}")
    await database.initialize()
    previous_valid_until = utc_now() + timedelta(days=20)
    current_valid_until = previous_valid_until + timedelta(days=30)
    order_id, _, public_guid = await _seed_applied_order(
        database,
        order_kind="extend",
        previous_valid_until=previous_valid_until,
        current_valid_until=current_valid_until,
        previous_max_devices=3,
        current_max_devices=3,
        include_before_snapshot=False,
    )
    mediator = RefundMediator(
        public_guid=public_guid,
        version=2,
        status="active",
        valid_until_utc=current_valid_until,
        max_device_tokens=3,
    )
    try:
        async with database.session() as session:
            service = PurchaseService(session, _settings(), mediator)
            candidate = await service.preview_refund(order_id, admin_telegram_id=1)
            assert candidate.is_eligible is True
            # Legacy base fields are an explicit conservative fallback and still produce a plan.
            assert candidate.confirmation_token is not None
            assert candidate.target_max_devices == 3
            assert to_aware_utc(candidate.target_valid_until_utc) == to_aware_utc(
                previous_valid_until
            )
        assert mediator.apply_calls == 0
    finally:
        await database.dispose()


@pytest.mark.asyncio
async def test_applied_referral_reward_is_reversed_without_removing_later_paid_time(
    tmp_path: Path,
) -> None:
    database = Database(f"sqlite+aiosqlite:///{tmp_path / 'referral-reversal.db'}")
    await database.initialize()
    now = utc_now()
    paid_expiry = now + timedelta(days=30)
    reward_expiry = paid_expiry + timedelta(days=10)
    public_guid = "00000000-0000-0000-0000-000000000778"
    try:
        async with database.session() as session:
            referrer = User(
                telegram_id=710001,
                username="referrer",
                first_name="Referrer",
                referral_code="referrer-code",
                created_at=now,
                updated_at=now,
            )
            referred = User(
                telegram_id=710002,
                username="referred",
                first_name="Referred",
                referred_by_user_id=None,
                referral_code="referred-code",
                created_at=now,
                updated_at=now,
            )
            session.add_all([referrer, referred])
            await session.flush()
            subscription = Subscription(
                user_id=referrer.id,
                public_guid=public_guid,
                signed_url="",
                max_devices=3,
                status="active",
                starts_at=now - timedelta(days=1),
                expires_at=reward_expiry,
                created_at=now,
                updated_at_utc=now,
            )
            session.add(subscription)
            await session.flush()
            referrer.primary_subscription_id = subscription.id
            session.add(
                AccessEntitlement(
                    subscription_id=subscription.id,
                    version=2,
                    status="active",
                    valid_until_utc=reward_expiry,
                    max_device_tokens=3,
                    updated_at_utc=now,
                )
            )
            source_order = Order(
                public_order_id="referral-source-refunded",
                user_id=referred.id,
                order_kind="purchase",
                status="refunded",
                duration_days=30,
                selected_max_devices=1,
                amount_minor_units=199,
                final_amount_minor_units=199,
                currency="XTR",
                provider="telegram_stars",
                provider_payment_id="referral-source-charge",
                invoice_payload="referral-source-payload",
                paid_at=now,
                completed_at_utc=now,
                created_at=now,
            )
            session.add(source_order)
            await session.flush()
            reward = ReferralReward(
                referrer_user_id=referrer.id,
                referred_user_id=referred.id,
                source_order_id=source_order.id,
                reward_percent=10,
                reward_duration_seconds=10 * 86400,
                status="reversal_required",
                available_at_utc=now - timedelta(hours=1),
                target_subscription_id=subscription.id,
                entitlement_version=2,
                idempotency_key=f"referral:{source_order.id}",
                created_at_utc=now - timedelta(days=1),
                applied_at_utc=now - timedelta(hours=1),
                previous_entitlement_version=1,
                previous_status="active",
                previous_valid_until_utc=paid_expiry,
                previous_max_devices=3,
            )
            session.add(reward)
            await session.flush()
            session.add(
                CommercialEntitlementAdjustment(
                    subscription_id=subscription.id,
                    source_kind="referral_reward",
                    duration_delta_seconds=10 * 86400,
                    device_limit_before=3,
                    device_limit_after=3,
                    source_entity_id=str(reward.id),
                    idempotency_key=reward.idempotency_key,
                    status="applied",
                    created_at_utc=now,
                    applied_at_utc=now,
                )
            )
            session.add(
                CommercialEntitlementSegment(
                    subscription_id=subscription.id,
                    source_kind="referral_reward",
                    starts_at_utc=paid_expiry,
                    ends_at_utc=reward_expiry,
                    source_entity_id=str(reward.id),
                    idempotency_key=reward.idempotency_key,
                    status="applied",
                    created_at_utc=now,
                )
            )
            reward_id = reward.id
            subscription_id = subscription.id

        mediator = RefundMediator(
            public_guid=public_guid,
            version=2,
            status="active",
            valid_until_utc=reward_expiry,
            max_device_tokens=3,
        )
        await process_referral_rewards_once(database.session, mediator)

        async with database.session() as session:
            reward = await session.get(ReferralReward, reward_id)
            subscription = await session.get(Subscription, subscription_id)
            assert reward is not None
            assert subscription is not None
            assert reward.status == "reversed"
            assert reward.reversed_at_utc is not None
            assert to_aware_utc(subscription.expires_at) == to_aware_utc(paid_expiry)
            assert subscription.status == "active"
            adjustment = await session.scalar(
                select(CommercialEntitlementAdjustment).where(
                    CommercialEntitlementAdjustment.source_entity_id == str(reward_id)
                )
            )
            segment = await session.scalar(
                select(CommercialEntitlementSegment).where(
                    CommercialEntitlementSegment.source_entity_id == str(reward_id)
                )
            )
            assert adjustment is not None and adjustment.status == "reversed"
            assert segment is not None and segment.status == "reversed"
        assert mediator.apply_calls == 1
    finally:
        await database.dispose()


@pytest.mark.asyncio
async def test_newer_entitlement_version_blocks_refund_before_provider_call(
    tmp_path: Path,
) -> None:
    database = Database(f"sqlite+aiosqlite:///{tmp_path / 'refund-stale-plan.db'}")
    await database.initialize()
    previous_valid_until = utc_now() + timedelta(days=20)
    current_valid_until = previous_valid_until + timedelta(days=30)
    order_id, subscription_id, public_guid = await _seed_applied_order(
        database,
        order_kind="extend",
        previous_valid_until=previous_valid_until,
        current_valid_until=current_valid_until,
        previous_max_devices=3,
        current_max_devices=3,
    )
    mediator = RefundMediator(
        public_guid=public_guid,
        version=2,
        status="active",
        valid_until_utc=current_valid_until,
        max_device_tokens=3,
    )
    try:
        async with database.session() as session:
            service = PurchaseService(session, _settings(), mediator)
            preview = await service.preview_refund(order_id, admin_telegram_id=1)
            assert preview.confirmation_token is not None

        async with database.session() as session:
            entitlement = await session.scalar(
                select(AccessEntitlement).where(
                    AccessEntitlement.subscription_id == subscription_id
                )
            )
            assert entitlement is not None
            entitlement.version = 3
            entitlement.valid_until_utc = current_valid_until + timedelta(days=30)
            await session.commit()

        async with database.session() as session:
            service = PurchaseService(session, _settings(), mediator)
            with pytest.raises(ValueError, match="refund_state_changed"):
                await service.confirm_refund(
                    preview.confirmation_token,
                    admin_telegram_id=1,
                )

        async with database.session() as session:
            order = await session.get(Order, order_id)
            operation = await session.scalar(
                select(RefundOperation).where(RefundOperation.order_id == order_id)
            )
            plan = await session.scalar(select(RefundPlan).where(RefundPlan.order_id == order_id))
            assert order is not None and order.status == "paid"
            assert operation is not None and operation.state == "manual_review"
            assert operation.provider_requested_at_utc is None
            assert plan is not None and plan.state == "manual_review"
            assert plan.failure_code == "required_entitlement_version_changed_before_provider"
        assert mediator.apply_calls == 0
    finally:
        await database.dispose()


@pytest.mark.asyncio
async def test_first_purchase_refund_disables_only_created_subscription(tmp_path: Path) -> None:
    database = Database(f"sqlite+aiosqlite:///{tmp_path / 'purchase-refund.db'}")
    await database.initialize()
    previous_valid_until = utc_now()
    current_valid_until = previous_valid_until + timedelta(days=30)
    order_id, subscription_id, public_guid = await _seed_applied_order(
        database,
        order_kind="purchase",
        previous_valid_until=previous_valid_until,
        current_valid_until=current_valid_until,
        previous_max_devices=0,
        current_max_devices=3,
    )
    mediator = RefundMediator(
        public_guid=public_guid,
        version=2,
        status="active",
        valid_until_utc=current_valid_until,
        max_device_tokens=3,
    )
    try:
        async with database.session() as session:
            service = PurchaseService(session, _settings(), mediator)
            preview = await service.preview_refund(order_id, admin_telegram_id=1)
            assert preview.is_eligible is True
            assert preview.target_status == "disabled"
            assert preview.confirmation_token is not None
            await service.confirm_refund(preview.confirmation_token, admin_telegram_id=1)
            await service.complete_refund_after_provider(order_id)

        async with database.session() as session:
            subscription = await session.get(Subscription, subscription_id)
            order = await session.get(Order, order_id)
            assert subscription is not None and subscription.status == "disabled"
            assert subscription.disabled_at is not None
            assert order is not None and order.status == "refunded"
            adjustment = await session.scalar(
                select(CommercialEntitlementAdjustment).where(
                    CommercialEntitlementAdjustment.source_order_id == order_id
                )
            )
            assert adjustment is not None and adjustment.status == "reversed"
        assert mediator.apply_calls == 1
    finally:
        await database.dispose()
