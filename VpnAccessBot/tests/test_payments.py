from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta

import pytest

import vpn_access_bot.services as services_module
from vpn_access_bot.config import Settings
from vpn_access_bot.constants import (
    ORDER_STATUS_ACTIVATION_FAILED,
    ORDER_STATUS_PAID,
    ORDER_STATUS_PAYMENT_RECEIVED,
    ORDER_STATUS_PENDING,
    ORDER_STATUS_REFUNDED,
    PAYMENT_MODE_MANUAL,
    PAYMENT_MODE_TELEGRAM_STARS,
    TELEGRAM_STARS_CURRENCY,
    TELEGRAM_STARS_PROVIDER_TOKEN,
)
from vpn_access_bot.mediator_client import (
    MediatorClientError,
    MediatorSubscriptionDetails,
)
from vpn_access_bot.models import (
    AccessEntitlement,
    CommercialEntitlementAdjustment,
    CommercialEntitlementSegment,
    EntitlementOperation,
    Order,
    OrderApplication,
    ReferralReward,
    Subscription,
    Tariff,
    TrialClaim,
    User,
    utc_now,
)
from vpn_access_bot.operations import AppliedEntitlement
from vpn_access_bot.product_catalog import ProductCatalog
from vpn_access_bot.services import PurchaseService


@dataclass
class FakeStore:
    users_by_telegram_id: dict[int, User] = field(default_factory=dict)
    tariffs_by_code: dict[str, Tariff] = field(default_factory=dict)
    orders_by_id: dict[int, Order] = field(default_factory=dict)
    orders_by_payload: dict[str, Order] = field(default_factory=dict)
    subscriptions_by_id: dict[int, Subscription] = field(default_factory=dict)
    entitlements_by_subscription_id: dict[int, AccessEntitlement] = field(default_factory=dict)
    applications_by_order_id: dict[int, OrderApplication] = field(default_factory=dict)
    commercial_adjustments_by_key: dict[str, CommercialEntitlementAdjustment] = field(
        default_factory=dict
    )
    commercial_segments_by_key: dict[str, CommercialEntitlementSegment] = field(
        default_factory=dict
    )
    trial_claims_by_user_id: dict[int, TrialClaim] = field(default_factory=dict)
    referral_rewards_by_order_id: dict[int, ReferralReward] = field(default_factory=dict)
    next_user_id: int = 1
    next_order_id: int = 1
    next_subscription_id: int = 1
    next_entitlement_id: int = 1
    next_application_id: int = 1
    next_adjustment_id: int = 1
    next_segment_id: int = 1
    next_referral_reward_id: int = 1
    next_payment_inbox_id: int = 1
    access_leases: dict[int, tuple[str, str]] = field(default_factory=dict)
    product_event_keys: set[str] = field(default_factory=set)
    entitlement_operations_by_source: dict[tuple[str, str, str], EntitlementOperation] = field(
        default_factory=dict
    )
    payment_inbox: dict[str, object] = field(default_factory=dict)
    outbox_keys: set[str] = field(default_factory=set)
    refund_operations_by_order: dict[int, object] = field(default_factory=dict)
    refund_plans_by_order: dict[int, object] = field(default_factory=dict)

    def __post_init__(self) -> None:
        self.tariffs_by_code["month_3_devices"] = Tariff(
            id=1,
            code="month_3_devices",
            title="1 месяц",
            description="Доступ на 30 дней, до 3 устройств.",
            price_minor_units=199,
            currency=TELEGRAM_STARS_CURRENCY,
            duration_days=30,
            max_devices=3,
            is_active=True,
            sort_order=10,
        )


@dataclass
class FakeSession:
    store: FakeStore

    async def commit(self) -> None:
        return None

    async def rollback(self) -> None:
        return None


class FakeUserRepository:
    def __init__(self, session: FakeSession) -> None:
        self._store = session.store

    async def get_by_telegram_id(self, telegram_id: int) -> User | None:
        return self._store.users_by_telegram_id.get(telegram_id)

    async def get_or_create_from_message_user(
        self,
        telegram_id: int,
        username: str | None,
        first_name: str | None,
    ) -> User:
        user = self._store.users_by_telegram_id.get(telegram_id)
        now = utc_now()

        if user is None:
            user = User(
                id=self._store.next_user_id,
                telegram_id=telegram_id,
                username=username,
                first_name=first_name,
                created_at=now,
                updated_at=now,
            )
            self._store.next_user_id += 1
            self._store.users_by_telegram_id[telegram_id] = user
            return user

        user.username = username
        user.first_name = first_name
        user.updated_at = now
        return user


class FakeTariffRepository:
    def __init__(self, session: FakeSession) -> None:
        self._store = session.store

    async def get_active_by_code(self, code: str) -> Tariff | None:
        tariff = self._store.tariffs_by_code.get(code)

        if tariff is None or not tariff.is_active:
            return None

        return tariff


class FakeOrderRepository:
    def __init__(self, session: FakeSession) -> None:
        self._store = session.store

    async def create_pending_order(
        self,
        user: User,
        tariff: Tariff,
        provider: str,
        amount_minor_units: int | None = None,
        currency: str | None = None,
    ) -> Order:
        order_id = self._store.next_order_id
        self._store.next_order_id += 1
        order = Order(
            id=order_id,
            public_order_id=f"public-order-{order_id}",
            user_id=user.id,
            tariff_id=tariff.id,
            status=ORDER_STATUS_PENDING,
            period_count=max(tariff.duration_days // 30, 1),
            duration_days=tariff.duration_days,
            selected_max_devices=tariff.max_devices,
            amount_minor_units=(
                amount_minor_units if amount_minor_units is not None else tariff.price_minor_units
            ),
            currency=(currency or tariff.currency).upper(),
            provider=provider,
            pricing_version="legacy-tariff",
            order_kind="purchase",
            invoice_payload=f"order:{order_id}",
            created_at=utc_now(),
        )
        order.user = user
        order.tariff = tariff
        self._store.orders_by_id[order.id] = order
        self._store.orders_by_payload[order.invoice_payload] = order
        return order

    async def get_for_payment_payload(self, payload: str) -> Order | None:
        return self._store.orders_by_payload.get(payload)

    async def get_for_payment_payload_for_user(self, payload: str, user_id: int) -> Order | None:
        order = self._store.orders_by_payload.get(payload)
        return order if order is not None and order.user_id == user_id else None

    async def get_by_id_for_user(self, order_id: int, user_id: int) -> Order | None:
        order = self._store.orders_by_id.get(order_id)
        return order if order is not None and order.user_id == user_id else None

    async def get_by_id(self, order_id: int) -> Order | None:
        return self._store.orders_by_id.get(order_id)

    async def get_for_quote(self, quote_id: int) -> Order | None:
        return next(
            (order for order in self._store.orders_by_id.values() if order.quote_id == quote_id),
            None,
        )

    async def get_by_provider_payment_id(
        self,
        provider: str,
        provider_payment_id: str,
    ) -> Order | None:
        return next(
            (
                order
                for order in self._store.orders_by_id.values()
                if order.provider == provider and order.provider_payment_id == provider_payment_id
            ),
            None,
        )

    async def claim_payment_bot(self, order: Order, payment_bot_key: str) -> bool:
        if order.payment_bot_key is None:
            order.payment_bot_key = payment_bot_key
            return True
        return order.payment_bot_key == payment_bot_key

    async def try_mark_checkout_authorized(
        self,
        order: Order,
        *,
        authorized_at_utc: datetime,
        authorized_until_utc: datetime,
    ) -> None:
        if order.status != ORDER_STATUS_PENDING:
            return False
        if order.expires_at_utc is not None and order.expires_at_utc <= authorized_at_utc:
            return False
        order.checkout_authorized_at_utc = authorized_at_utc
        order.checkout_authorized_until_utc = authorized_until_utc
        return True

    async def mark_payment_received(
        self,
        order: Order,
        provider_payment_id: str,
        paid_at: datetime | None = None,
    ) -> None:
        order.status = ORDER_STATUS_PAYMENT_RECEIVED
        order.provider_payment_id = provider_payment_id
        order.paid_at = paid_at or utc_now()

    async def mark_complimentary_ready(self, order: Order) -> None:
        order.status = ORDER_STATUS_PAYMENT_RECEIVED
        order.provider_payment_id = None
        order.paid_at = None

    async def mark_activating(self, order: Order) -> None:
        order.status = "activating"

    async def mark_paid(self, order: Order, provider_payment_id: str | None = None) -> None:
        order.status = ORDER_STATUS_PAID

        if provider_payment_id is not None:
            order.provider_payment_id = provider_payment_id

        if order.amount_minor_units > 0 and order.paid_at is None:
            order.paid_at = utc_now()

    async def mark_activation_failed(
        self,
        order: Order,
        error_code: str = "activation_failed",
    ) -> None:
        order.status = ORDER_STATUS_ACTIVATION_FAILED
        order.last_activation_error_code = error_code

    async def mark_refunded(self, order: Order) -> None:
        order.status = ORDER_STATUS_REFUNDED

    async def mark_expired(self, order: Order) -> None:
        order.status = "expired"

    async def transition_status(
        self,
        order_id: int,
        expected_statuses: list[str],
        next_status: str,
    ) -> bool:
        order = self._store.orders_by_id.get(order_id)

        if order is None or order.status not in expected_statuses:
            return False

        order.status = next_status
        return True


class FakeSubscriptionRepository:
    def __init__(self, session: FakeSession) -> None:
        self._store = session.store

    async def get_latest_for_user(self, user_id: int) -> Subscription | None:
        subscriptions = [
            subscription
            for subscription in self._store.subscriptions_by_id.values()
            if subscription.user_id == user_id
        ]

        if not subscriptions:
            return None

        return sorted(subscriptions, key=lambda item: (item.created_at, item.id))[-1]

    async def create(
        self,
        user: User,
        tariff: Tariff | None,
        public_guid: str,
        expires_at: datetime,
        max_devices: int | None = None,
    ) -> Subscription:
        subscription = Subscription(
            id=self._store.next_subscription_id,
            user_id=user.id,
            tariff_id=tariff.id if tariff else None,
            public_guid=public_guid,
            signed_url="",
            max_devices=(
                max_devices if max_devices is not None else (tariff.max_devices if tariff else 1)
            ),
            status="active",
            starts_at=utc_now(),
            expires_at=expires_at,
            created_at=utc_now(),
            updated_at_utc=utc_now(),
        )
        subscription.user = user
        subscription.tariff = tariff
        self._store.next_subscription_id += 1
        self._store.subscriptions_by_id[subscription.id] = subscription
        return subscription

    async def extend(
        self,
        subscription: Subscription,
        tariff: Tariff | None,
        new_expires_at: datetime,
        max_devices: int | None = None,
    ) -> None:
        subscription.tariff = tariff
        subscription.tariff_id = tariff.id if tariff else subscription.tariff_id
        subscription.max_devices = (
            max_devices if max_devices is not None else subscription.max_devices
        )
        subscription.status = "active"
        subscription.expires_at = new_expires_at
        subscription.disabled_at = None
        subscription.updated_at_utc = utc_now()

    async def get_by_id(self, subscription_id: int) -> Subscription | None:
        return self._store.subscriptions_by_id.get(subscription_id)


class FakeEntitlementRepository:
    def __init__(self, session: FakeSession) -> None:
        self._store = session.store

    async def get_for_subscription(self, subscription_id: int) -> AccessEntitlement | None:
        return self._store.entitlements_by_subscription_id.get(subscription_id)

    async def upsert(
        self,
        subscription: Subscription,
        status: str,
        valid_until_utc: datetime,
        max_device_tokens: int,
    ) -> AccessEntitlement:
        existing = self._store.entitlements_by_subscription_id.get(subscription.id)

        if existing is None:
            existing = AccessEntitlement(
                id=self._store.next_entitlement_id,
                subscription_id=subscription.id,
                version=1,
                status=status,
                valid_until_utc=valid_until_utc,
                max_device_tokens=max_device_tokens,
                updated_at_utc=utc_now(),
            )
            self._store.next_entitlement_id += 1
            self._store.entitlements_by_subscription_id[subscription.id] = existing
            return existing

        existing.version += 1
        existing.status = status
        existing.valid_until_utc = valid_until_utc
        existing.max_device_tokens = max_device_tokens
        existing.updated_at_utc = utc_now()
        return existing

    async def set_authoritative(
        self,
        subscription: Subscription,
        *,
        version: int,
        status: str,
        valid_until_utc: datetime,
        max_device_tokens: int,
    ) -> AccessEntitlement:
        existing = await self.get_for_subscription(subscription.id)
        if existing is None:
            existing = AccessEntitlement(
                id=self._store.next_entitlement_id,
                subscription_id=subscription.id,
                version=version,
                status=status,
                valid_until_utc=valid_until_utc,
                max_device_tokens=max_device_tokens,
                updated_at_utc=utc_now(),
            )
            self._store.next_entitlement_id += 1
            self._store.entitlements_by_subscription_id[subscription.id] = existing
            return existing
        existing.version = version
        existing.status = status
        existing.valid_until_utc = valid_until_utc
        existing.max_device_tokens = max_device_tokens
        existing.updated_at_utc = utc_now()
        return existing


class FakeOrderApplicationRepository:
    def __init__(self, session: FakeSession) -> None:
        self._store = session.store

    async def get_for_order(self, order_id: int) -> OrderApplication | None:
        return self._store.applications_by_order_id.get(order_id)

    async def create(
        self,
        order: Order,
        subscription: Subscription,
        resulting_valid_until_utc: datetime,
        resulting_entitlement_version: int,
        *,
        previous_entitlement_version: int | None = None,
        previous_status: str | None = None,
        previous_valid_until_utc: datetime | None = None,
        previous_max_devices: int | None = None,
    ) -> OrderApplication:
        application = OrderApplication(
            id=self._store.next_application_id,
            order_id=order.id,
            subscription_id=subscription.id,
            applied_at_utc=utc_now(),
            duration_days=order.duration_days,
            selected_max_devices=order.selected_max_devices,
            resulting_valid_until_utc=resulting_valid_until_utc,
            resulting_entitlement_version=resulting_entitlement_version,
            previous_entitlement_version=previous_entitlement_version,
            previous_status=previous_status,
            previous_valid_until_utc=previous_valid_until_utc,
            previous_max_devices=previous_max_devices,
        )
        self._store.next_application_id += 1
        self._store.applications_by_order_id[order.id] = application
        return application

    async def has_later_application(
        self,
        subscription_id: int,
        applied_after_utc: datetime,
        exclude_order_id: int,
    ) -> bool:
        return any(
            application.subscription_id == subscription_id
            and application.order_id != exclude_order_id
            and application.applied_at_utc > applied_after_utc
            for application in self._store.applications_by_order_id.values()
        )


class FakeCommercialEntitlementAdjustmentRepository:
    def __init__(self, session: FakeSession) -> None:
        self._store = session.store

    async def create_applied_once(
        self,
        subscription: Subscription,
        source_kind: str,
        duration_delta_seconds: int,
        device_limit_before: int,
        device_limit_after: int,
        idempotency_key: str,
        source_order_id: int | None = None,
        source_entity_id: str | None = None,
    ) -> CommercialEntitlementAdjustment:
        existing = self._store.commercial_adjustments_by_key.get(idempotency_key)

        if existing is not None:
            return existing

        adjustment = CommercialEntitlementAdjustment(
            id=self._store.next_adjustment_id,
            subscription_id=subscription.id,
            source_kind=source_kind,
            duration_delta_seconds=duration_delta_seconds,
            device_limit_before=device_limit_before,
            device_limit_after=device_limit_after,
            source_order_id=source_order_id,
            source_entity_id=source_entity_id,
            idempotency_key=idempotency_key,
            status="applied",
            created_at_utc=utc_now(),
            applied_at_utc=utc_now(),
        )
        self._store.next_adjustment_id += 1
        self._store.commercial_adjustments_by_key[idempotency_key] = adjustment
        return adjustment

    async def mark_reversed_for_order(self, order_id: int) -> int:
        count = 0
        for adjustment in self._store.commercial_adjustments_by_key.values():
            if adjustment.source_order_id == order_id and adjustment.status == "applied":
                adjustment.status = "reversed"
                count += 1
        return count


class FakeDiscountRedemptionRepository:
    def __init__(self, session: FakeSession) -> None:
        self._store = session.store

    async def reserve_for_order(self, order: Order) -> None:
        _ = order

    async def apply_for_order(self, order_id: int) -> None:
        _ = order_id

    async def release_for_order(self, order_id: int) -> None:
        _ = order_id

    async def restore_for_paid_order(self, order_id: int) -> None:
        _ = order_id


class FakeCommercialEntitlementSegmentRepository:
    def __init__(self, session: FakeSession) -> None:
        self._store = session.store

    async def create_applied_once(
        self,
        *,
        subscription_id: int,
        source_kind: str,
        starts_at_utc: datetime,
        ends_at_utc: datetime,
        idempotency_key: str,
        source_order_id: int | None = None,
        source_entity_id: str | None = None,
    ) -> CommercialEntitlementSegment:
        existing = self._store.commercial_segments_by_key.get(idempotency_key)

        if existing is not None:
            return existing

        segment = CommercialEntitlementSegment(
            id=self._store.next_segment_id,
            subscription_id=subscription_id,
            source_kind=source_kind,
            starts_at_utc=starts_at_utc,
            ends_at_utc=ends_at_utc,
            source_order_id=source_order_id,
            source_entity_id=source_entity_id,
            idempotency_key=idempotency_key,
            status="applied",
            created_at_utc=utc_now(),
        )
        self._store.next_segment_id += 1
        self._store.commercial_segments_by_key[idempotency_key] = segment
        return segment

    async def remaining_paid_seconds(
        self,
        subscription: Subscription,
        now: datetime | None = None,
    ) -> int:
        reference = now or utc_now()
        return sum(
            max(
                int((segment.ends_at_utc - max(segment.starts_at_utc, reference)).total_seconds()),
                0,
            )
            for segment in self._store.commercial_segments_by_key.values()
            if segment.subscription_id == subscription.id
            and segment.source_kind == "paid_order"
            and segment.status == "applied"
            and segment.ends_at_utc > reference
        )

    async def mark_reversed_for_order(self, order_id: int) -> int:
        count = 0
        for segment in self._store.commercial_segments_by_key.values():
            if segment.source_order_id == order_id and segment.status == "applied":
                segment.status = "reversed"
                segment.reversed_at_utc = utc_now()
                count += 1
        return count


class FakeTrialClaimRepository:
    def __init__(self, session: FakeSession) -> None:
        self._store = session.store

    async def get_for_user(self, user_id: int) -> TrialClaim | None:
        return self._store.trial_claims_by_user_id.get(user_id)


class FakeReferralRewardRepository:
    def __init__(self, session: FakeSession) -> None:
        self._store = session.store

    async def create_once(
        self,
        referrer_user_id: int,
        referred_user_id: int,
        source_order_id: int,
        reward_percent: int,
        reward_duration_seconds: int,
        available_at_utc: datetime,
    ) -> ReferralReward:
        existing = self._store.referral_rewards_by_order_id.get(source_order_id)

        if existing is not None:
            return existing

        reward = ReferralReward(
            id=self._store.next_referral_reward_id,
            referrer_user_id=referrer_user_id,
            referred_user_id=referred_user_id,
            source_order_id=source_order_id,
            reward_percent=reward_percent,
            reward_duration_seconds=reward_duration_seconds,
            status="pending_hold",
            available_at_utc=available_at_utc,
            idempotency_key=f"referral:{source_order_id}",
            created_at_utc=utc_now(),
        )
        self._store.next_referral_reward_id += 1
        self._store.referral_rewards_by_order_id[source_order_id] = reward
        return reward

    async def get_for_source_order(self, source_order_id: int) -> ReferralReward | None:
        return self._store.referral_rewards_by_order_id.get(source_order_id)

    async def cancel_unapplied_for_source_order(self, source_order_id: int) -> str | None:
        reward = await self.get_for_source_order(source_order_id)
        if reward is None:
            return None
        if reward.status in {"pending_hold", "available", "failed"}:
            reward.status = "cancelled"
            reward.cancelled_at_utc = utc_now()
        elif reward.status in {"applying", "applied"}:
            reward.status = "reversal_required"
        return reward.status


class FakeAccessOperationLeaseRepository:
    def __init__(self, session: FakeSession) -> None:
        self._store = session.store

    async def acquire(
        self,
        *,
        user_id: int,
        owner_kind: str,
        owner_key: str,
        lease_seconds: int = 120,
    ) -> bool:
        _ = lease_seconds
        current = self._store.access_leases.get(user_id)
        if current is not None:
            return False
        self._store.access_leases[user_id] = (owner_kind, owner_key)
        return True

    async def renew(
        self,
        *,
        user_id: int,
        owner_key: str,
        lease_seconds: int = 120,
    ) -> bool:
        _ = lease_seconds
        current = self._store.access_leases.get(user_id)
        return current is not None and current[1] == owner_key

    async def release(self, *, user_id: int, owner_key: str) -> None:
        current = self._store.access_leases.get(user_id)
        if current is not None and current[1] == owner_key:
            self._store.access_leases.pop(user_id, None)


class FakePaymentInboxRepository:
    def __init__(self, session: FakeSession) -> None:
        self._store = session.store

    async def receive(self, **kwargs):
        charge = str(kwargs["provider_charge_id"])
        item = self._store.payment_inbox.get(charge)
        inserted = item is None
        if item is None:
            item = type(
                "PaymentInboxItem",
                (),
                {
                    "id": self._store.next_payment_inbox_id,
                    "provider": str(kwargs["provider"]),
                    "provider_charge_id": charge,
                    "invoice_payload": str(kwargs["invoice_payload"]),
                    "payer_external_id": str(kwargs["payer_external_id"]),
                    "amount_minor_units": int(kwargs["amount_minor_units"]),
                    "currency": str(kwargs["currency"]).upper(),
                    "received_at_utc": utc_now(),
                    "provider_occurred_at_utc": kwargs.get("provider_occurred_at_utc"),
                    "origin_bot_key": kwargs.get("origin_bot_key"),
                    "reconciliation_status": "received",
                    "matched_order_id": None,
                    "failure_code": None,
                    "processed_at_utc": None,
                },
            )()
            self._store.next_payment_inbox_id += 1
            self._store.payment_inbox[charge] = item
        else:
            same_evidence = (
                item.provider == str(kwargs["provider"])
                and item.invoice_payload == str(kwargs["invoice_payload"])
                and item.payer_external_id == str(kwargs["payer_external_id"])
                and item.amount_minor_units == int(kwargs["amount_minor_units"])
                and item.currency == str(kwargs["currency"]).upper()
            )
            if not same_evidence:
                item.reconciliation_status = "manual_review"
                item.failure_code = "provider_charge_evidence_conflict"
        return item, inserted

    async def get_by_id(self, inbox_id: int):
        return next(
            (item for item in self._store.payment_inbox.values() if item.id == inbox_id),
            None,
        )

    async def mark_matched(self, inbox, order_id: int) -> None:
        inbox.matched_order_id = order_id
        inbox.reconciliation_status = "matched"
        inbox.failure_code = None

    async def mark_applied(self, inbox, order_id: int) -> None:
        inbox.matched_order_id = order_id
        inbox.reconciliation_status = "applied"
        inbox.failure_code = None
        inbox.processed_at_utc = utc_now()

    async def mark_applied_for_order(self, order_id: int) -> None:
        for inbox in self._store.payment_inbox.values():
            if inbox.matched_order_id == order_id and inbox.reconciliation_status == "matched":
                await self.mark_applied(inbox, order_id)

    async def mark_manual_review(self, inbox, failure_code: str) -> None:
        inbox.reconciliation_status = "manual_review"
        inbox.failure_code = failure_code
        inbox.processed_at_utc = utc_now()


class FakeEntitlementOperationRepository:
    def __init__(self, session: FakeSession) -> None:
        self._store = session.store

    async def get_by_public_id(self, public_id: str):
        return next(
            (
                operation
                for operation in self._store.entitlement_operations_by_source.values()
                if operation.public_id == public_id
            ),
            None,
        )

    async def get_for_source(self, *, source_entity_type, source_entity_id, operation_type):
        return self._store.entitlement_operations_by_source.get(
            (source_entity_type, source_entity_id, operation_type)
        )

    async def mark_completed(self, operation) -> None:
        operation.state = "completed"
        operation.local_commit_completed_at_utc = utc_now()


class FakeEntitlementOperationCoordinator:
    def __init__(self, session: FakeSession, mediator_client) -> None:
        self._session = session
        self._store = session.store
        self._mediator_client = mediator_client

    async def prepare_order(self, order: Order) -> EntitlementOperation:
        operation_type = "paid_activation" if order.amount_minor_units > 0 else "complimentary"
        key = ("order", order.public_order_id, operation_type)
        existing = self._store.entitlement_operations_by_source.get(key)
        if existing is not None:
            return existing
        duration_days = (
            order.purchased_duration_days
            if order.purchased_duration_days is not None
            else (order.requested_duration_days or order.duration_days)
        )
        operation = EntitlementOperation(
            id=len(self._store.entitlement_operations_by_source) + 1,
            public_id=f"operation-{order.public_order_id}",
            subscription_id=order.target_subscription_id,
            user_id=order.user_id,
            operation_type=operation_type,
            source_entity_type="order",
            source_entity_id=order.public_order_id,
            idempotency_key=f"entitlement:order:{order.public_order_id}",
            duration_delta_seconds=max(duration_days, 0) * 86400,
            requested_device_limit=order.requested_max_devices or order.selected_max_devices,
            requested_status="active",
            state="pending",
            attempt_count=0,
            created_at_utc=utc_now(),
            updated_at_utc=utc_now(),
        )
        self._store.entitlement_operations_by_source[key] = operation
        return operation

    async def apply_order(self, operation, order, target_subscription):
        duration = timedelta(seconds=operation.duration_delta_seconds)
        if target_subscription is None:
            valid_until = utc_now() + duration
            result = await self._mediator_client.create_subscription(
                external_request_id=operation.public_id,
                customer_reference=f"telegram:{order.user.telegram_id}",
                note=f"operation:{operation.public_id}",
                entitlement=type(
                    "Payload",
                    (),
                    {
                        "version": 1,
                        "status": "active",
                        "valid_until_utc": valid_until.isoformat(),
                        "max_device_tokens": operation.requested_device_limit,
                    },
                )(),
            )
            operation.state = "external_applied"
            operation.external_result_version = 1
            operation.external_result_status = "active"
            operation.external_result_valid_until_utc = valid_until
            operation.external_result_device_limit = operation.requested_device_limit
            operation.external_applied_at_utc = utc_now()
            return AppliedEntitlement(
                public_guid=result.public_guid,
                version=1,
                status="active",
                valid_until_utc=valid_until,
                max_device_tokens=operation.requested_device_limit,
                applied_at_utc=operation.external_applied_at_utc,
                created_subscription=True,
            )
        current_entitlement = self._store.entitlements_by_subscription_id.get(
            target_subscription.id
        )
        version = (current_entitlement.version if current_entitlement else 0) + 1
        valid_until = max(target_subscription.expires_at, utc_now()) + duration
        max_devices = max(target_subscription.max_devices, operation.requested_device_limit)
        await self._mediator_client.update_entitlement(
            target_subscription.public_guid,
            type(
                "Payload",
                (),
                {
                    "version": version,
                    "status": "active",
                    "valid_until_utc": valid_until.isoformat(),
                    "max_device_tokens": max_devices,
                },
            )(),
        )
        operation.state = "external_applied"
        operation.external_result_version = version
        operation.external_result_status = "active"
        operation.external_result_valid_until_utc = valid_until
        operation.external_result_device_limit = max_devices
        operation.external_applied_at_utc = utc_now()
        return AppliedEntitlement(
            public_guid=target_subscription.public_guid,
            version=version,
            status="active",
            valid_until_utc=valid_until,
            max_device_tokens=max_devices,
            applied_at_utc=operation.external_applied_at_utc,
        )

    async def prepare_generic(
        self,
        *,
        user_id,
        subscription_id,
        operation_type,
        source_entity_type,
        source_entity_id,
        duration_delta_seconds,
        requested_device_limit,
        requested_status,
        observed_valid_until_utc=None,
    ):
        key = (source_entity_type, source_entity_id, operation_type)
        existing = self._store.entitlement_operations_by_source.get(key)
        if existing is not None:
            return existing
        operation = EntitlementOperation(
            id=len(self._store.entitlement_operations_by_source) + 1,
            public_id=f"operation-{operation_type}-{source_entity_id}",
            subscription_id=subscription_id,
            user_id=user_id,
            operation_type=operation_type,
            source_entity_type=source_entity_type,
            source_entity_id=source_entity_id,
            idempotency_key=f"entitlement:{operation_type}:{source_entity_id}",
            duration_delta_seconds=duration_delta_seconds,
            requested_device_limit=requested_device_limit,
            requested_status=requested_status,
            observed_valid_until_utc=observed_valid_until_utc,
            state="pending",
            attempt_count=0,
            created_at_utc=utc_now(),
            updated_at_utc=utc_now(),
        )
        self._store.entitlement_operations_by_source[key] = operation
        return operation

    async def apply_generic(self, operation, subscription, **kwargs):
        _ = kwargs
        current = self._store.entitlements_by_subscription_id[subscription.id]
        version = current.version + 1
        await self._mediator_client.update_entitlement(
            subscription.public_guid,
            type(
                "Payload",
                (),
                {
                    "version": version,
                    "status": operation.requested_status,
                    "valid_until_utc": subscription.expires_at.isoformat(),
                    "max_device_tokens": subscription.max_devices,
                },
            )(),
        )
        operation.state = "external_applied"
        operation.external_result_version = version
        operation.external_result_status = operation.requested_status
        operation.external_result_valid_until_utc = subscription.expires_at
        operation.external_result_device_limit = subscription.max_devices
        operation.external_applied_at_utc = utc_now()
        return AppliedEntitlement(
            public_guid=subscription.public_guid,
            version=version,
            status=operation.requested_status,
            valid_until_utc=subscription.expires_at,
            max_device_tokens=subscription.max_devices,
            applied_at_utc=operation.external_applied_at_utc,
        )


class FakeRefundOperationRepository:
    def __init__(self, session: FakeSession) -> None:
        self._store = session.store

    async def get_for_order(self, order_id: int):
        return self._store.refund_operations_by_order.get(order_id)

    async def create_once(self, *, order, subscription_id, provider_charge_id):
        _ = provider_charge_id
        existing = self._store.refund_operations_by_order.get(order.id)
        if existing is not None:
            return existing
        operation = type(
            "RefundOperationItem",
            (),
            {
                "id": order.id,
                "public_id": f"refund-{order.public_order_id}",
                "order_id": order.id,
                "subscription_id": subscription_id,
                "state": "prepared",
                "compensation_operation_id": None,
                "last_error_code": None,
            },
        )()
        self._store.refund_operations_by_order[order.id] = operation
        return operation

    async def mark_provider_requested(self, operation) -> None:
        operation.state = "provider_unknown"

    async def mark_provider_refunded(self, operation) -> None:
        operation.state = "provider_refunded"

    async def mark_compensation_pending(self, operation, entitlement_operation_id) -> None:
        operation.state = "compensation_pending"
        operation.compensation_operation_id = entitlement_operation_id

    async def mark_completed(self, operation) -> None:
        operation.state = "completed"

    async def mark_manual_review(self, operation, error_code) -> None:
        operation.state = "manual_review"
        operation.last_error_code = error_code


class FakeRefundPlanRepository:
    def __init__(self, session: FakeSession) -> None:
        self._store = session.store

    async def get_for_order(self, order_id: int):
        return self._store.refund_plans_by_order.get(order_id)

    async def get_by_confirmation_hash(self, token_hash: str):
        return next(
            (
                plan
                for plan in self._store.refund_plans_by_order.values()
                if plan.confirmation_token_hash == token_hash
            ),
            None,
        )

    async def create_or_refresh(self, *, operation, order, **values):
        plan = self._store.refund_plans_by_order.get(order.id)
        now = utc_now()
        if plan is None:
            plan = type(
                "RefundPlanItem",
                (),
                {
                    "id": order.id,
                    "public_id": f"plan-{order.public_order_id}",
                    "refund_operation_id": operation.id,
                    "order_id": order.id,
                    "state": "prepared",
                    "confirmed_at_utc": None,
                    "failure_code": None,
                    "created_at_utc": now,
                },
            )()
            self._store.refund_plans_by_order[order.id] = plan
        for key, value in values.items():
            setattr(plan, key, value)
        plan.updated_at_utc = now
        return plan

    async def mark_confirmed(self, plan) -> None:
        plan.state = "confirmed"
        plan.confirmed_at_utc = utc_now()
        plan.confirmation_token_hash = None
        plan.updated_at_utc = utc_now()

    async def mark_state(self, plan, state: str, failure_code: str | None = None) -> None:
        plan.state = state
        plan.failure_code = failure_code
        plan.updated_at_utc = utc_now()


class FakeNotificationOutboxRepository:
    def __init__(self, session: FakeSession) -> None:
        self._store = session.store

    async def enqueue_once(self, *, idempotency_key: str, **kwargs):
        _ = kwargs
        self._store.outbox_keys.add(idempotency_key)
        return object()


class FakeProductEventRepository:
    def __init__(self, session: FakeSession) -> None:
        self._store = session.store

    async def record(
        self,
        *,
        event_name: str,
        user_id: int | None = None,
        idempotency_key: str | None = None,
        correlation_id: str | None = None,
        payload: dict[str, object] | None = None,
    ):
        _ = (event_name, user_id, correlation_id, payload)
        if idempotency_key is not None:
            self._store.product_event_keys.add(idempotency_key)
        return object()


class FakeMediatorClient:
    def __init__(self) -> None:
        self.fail_next_rent = False
        self.rent_calls = 0
        self.set_limit_calls = 0
        self.enable_calls = 0
        self._subscriptions_by_note: dict[str, MediatorSubscriptionDetails] = {}

    async def create_subscription(
        self,
        external_request_id: str,
        customer_reference: str,
        note: str,
        entitlement,
    ):
        self.rent_calls += 1

        if self.fail_next_rent:
            self.fail_next_rent = False
            raise MediatorClientError("rent failed")

        public_guid = f"00000000-0000-0000-0000-{self.rent_calls:012d}"

        self._subscriptions_by_note[note] = MediatorSubscriptionDetails(
            public_guid=public_guid,
            subscription_url=None,
            max_devices=entitlement.max_device_tokens,
            is_active=True,
            active_device_count=0,
            customer_name=customer_reference,
            note=note,
            devices=[],
        )

        return type(
            "CreateResult",
            (),
            {
                "public_guid": public_guid,
                "already_existed": False,
            },
        )()

    async def update_entitlement(self, public_guid: str, entitlement):
        self.set_limit_calls += 1
        self.enable_calls += 1
        return type(
            "EntitlementResult",
            (),
            {"status": "applied", "current_version": entitlement.version},
        )()

    async def find_subscription_by_note(self, note: str) -> MediatorSubscriptionDetails | None:
        return self._subscriptions_by_note.get(note)

    async def set_limit(self, public_guid: str, max_devices: int) -> None:
        self.set_limit_calls += 1

    async def enable_subscription(self, public_guid: str) -> None:
        self.enable_calls += 1


@pytest.fixture
def store(monkeypatch: pytest.MonkeyPatch) -> FakeStore:
    fake_store = FakeStore()
    monkeypatch.setattr(services_module, "UserRepository", FakeUserRepository)
    monkeypatch.setattr(services_module, "TariffRepository", FakeTariffRepository)
    monkeypatch.setattr(services_module, "OrderRepository", FakeOrderRepository)
    monkeypatch.setattr(services_module, "SubscriptionRepository", FakeSubscriptionRepository)
    monkeypatch.setattr(services_module, "EntitlementRepository", FakeEntitlementRepository)
    monkeypatch.setattr(
        services_module,
        "OrderApplicationRepository",
        FakeOrderApplicationRepository,
    )
    monkeypatch.setattr(
        services_module,
        "CommercialEntitlementAdjustmentRepository",
        FakeCommercialEntitlementAdjustmentRepository,
    )
    monkeypatch.setattr(
        services_module,
        "DiscountRedemptionRepository",
        FakeDiscountRedemptionRepository,
    )
    monkeypatch.setattr(
        services_module,
        "CommercialEntitlementSegmentRepository",
        FakeCommercialEntitlementSegmentRepository,
    )
    monkeypatch.setattr(services_module, "TrialClaimRepository", FakeTrialClaimRepository)
    monkeypatch.setattr(services_module, "ReferralRewardRepository", FakeReferralRewardRepository)
    monkeypatch.setattr(
        services_module,
        "AccessOperationLeaseRepository",
        FakeAccessOperationLeaseRepository,
    )
    monkeypatch.setattr(services_module, "ProductEventRepository", FakeProductEventRepository)
    monkeypatch.setattr(services_module, "PaymentInboxRepository", FakePaymentInboxRepository)
    monkeypatch.setattr(
        services_module,
        "EntitlementOperationRepository",
        FakeEntitlementOperationRepository,
    )
    monkeypatch.setattr(
        services_module,
        "EntitlementOperationCoordinator",
        FakeEntitlementOperationCoordinator,
    )
    monkeypatch.setattr(
        services_module,
        "NotificationOutboxRepository",
        FakeNotificationOutboxRepository,
    )
    monkeypatch.setattr(
        services_module,
        "RefundOperationRepository",
        FakeRefundOperationRepository,
    )
    monkeypatch.setattr(
        services_module,
        "RefundPlanRepository",
        FakeRefundPlanRepository,
    )
    return fake_store


def make_settings(
    payment_mode: str = PAYMENT_MODE_TELEGRAM_STARS,
    **overrides: object,
) -> Settings:
    values: dict[str, object] = {
        "TELEGRAM_BOT_TOKEN": "test-token",
        "ADMIN_TELEGRAM_IDS": "1",
        "PAYMENT_MODE": payment_mode,
        "MEDIATOR_BASE_URL": "http://127.0.0.1:5062",
        "MEDIATOR_ADMIN_TOKEN": "test-admin-token",
    }
    values.update(overrides)
    return Settings(**values)


async def create_order(
    store: FakeStore,
    settings: Settings,
    mediator_client: FakeMediatorClient,
    telegram_id: int = 100,
) -> tuple[int, str, int, str]:
    service = PurchaseService(FakeSession(store), settings, mediator_client)  # type: ignore[arg-type]
    order = await service.create_order_for_tariff(
        telegram_id=telegram_id,
        username="alice",
        first_name="Alice",
        tariff_code="month_3_devices",
    )
    return order.id, order.invoice_payload, order.amount_minor_units, order.currency


@pytest.mark.asyncio
async def test_complimentary_personal_discount_order_skips_invoice_and_payment(
    store: FakeStore,
) -> None:
    settings = make_settings()
    mediator_client = FakeMediatorClient()
    user = User(
        id=store.next_user_id,
        telegram_id=101,
        username="gift-user",
        first_name="Gift",
        referral_code="gift-user-code",
        created_at=utc_now(),
        updated_at=utc_now(),
    )
    store.next_user_id += 1
    store.users_by_telegram_id[user.telegram_id] = user
    order = Order(
        id=store.next_order_id,
        public_order_id="complimentary-order",
        user_id=user.id,
        status=ORDER_STATUS_PENDING,
        period_count=1,
        duration_days=30,
        selected_max_devices=1,
        requested_max_devices=1,
        requested_duration_days=30,
        amount_minor_units=0,
        final_amount_minor_units=0,
        price_before_personal_discount=100,
        personal_discount_id=1,
        personal_discount_bps=10_000,
        personal_discount_amount_minor_units=100,
        referral_eligible=False,
        currency=TELEGRAM_STARS_CURRENCY,
        provider=PAYMENT_MODE_TELEGRAM_STARS,
        pricing_version="test",
        order_kind="purchase",
        invoice_payload="complimentary-payload",
        created_at=utc_now(),
    )
    order.user = user
    store.next_order_id += 1
    store.orders_by_id[order.id] = order
    store.orders_by_payload[order.invoice_payload] = order
    service = PurchaseService(FakeSession(store), settings, mediator_client)  # type: ignore[arg-type]

    with pytest.raises(ValueError, match="must not create"):
        service.build_telegram_stars_invoice(order)

    prepared = await service.prepare_complimentary_order_for_activation(
        order.id, actor_telegram_id=101
    )

    assert prepared.needs_activation is True
    assert order.status == ORDER_STATUS_PAYMENT_RECEIVED
    assert order.provider_payment_id is None
    assert order.paid_at is None

    activation = await service.activate_order_by_id(order.id)

    assert activation.activated is True
    assert activation.subscription is not None
    assert order.status == ORDER_STATUS_PAID
    assert order.paid_at is None
    assert mediator_client.rent_calls == 1
    assert {item.source_kind for item in store.commercial_adjustments_by_key.values()} == {
        "complimentary_order"
    }
    assert {item.source_kind for item in store.commercial_segments_by_key.values()} == {
        "complimentary_order"
    }
    assert store.referral_rewards_by_order_id == {}


@pytest.mark.asyncio
async def test_telegram_stars_invoice_and_payment_state_machine(store: FakeStore) -> None:
    settings = make_settings()
    mediator_client = FakeMediatorClient()
    service = PurchaseService(FakeSession(store), settings, mediator_client)  # type: ignore[arg-type]
    order = await service.create_order_for_tariff(
        telegram_id=100,
        username="alice",
        first_name="Alice",
        tariff_code="month_3_devices",
    )
    invoice = service.build_telegram_stars_invoice(order)

    assert invoice.provider_token == TELEGRAM_STARS_PROVIDER_TOKEN
    assert invoice.currency == TELEGRAM_STARS_CURRENCY
    assert invoice.title == "Razaltush VPN"
    assert "1 месяц" in invoice.description
    assert len(invoice.prices) == 1
    assert invoice.prices[0].amount == 199

    payment = await service.record_successful_telegram_stars_payment(
        payload=order.invoice_payload,
        amount_minor_units=199,
        currency=TELEGRAM_STARS_CURRENCY,
        telegram_payment_charge_id="tg-charge-1",
        payer_telegram_id=100,
    )

    assert payment.needs_activation is True
    assert order.status == ORDER_STATUS_PAYMENT_RECEIVED

    activation = await service.activate_order_by_id(order.id)

    assert activation.activated is True
    assert activation.subscription is not None
    assert order.status == ORDER_STATUS_PAID
    assert mediator_client.rent_calls == 1


@pytest.mark.asyncio
async def test_duplicate_successful_payment_does_not_activate_twice(store: FakeStore) -> None:
    settings = make_settings()
    mediator_client = FakeMediatorClient()
    order_id, payload, amount, currency = await create_order(store, settings, mediator_client)
    service = PurchaseService(FakeSession(store), settings, mediator_client)  # type: ignore[arg-type]

    await service.record_successful_telegram_stars_payment(
        payload=payload,
        amount_minor_units=amount,
        currency=currency,
        telegram_payment_charge_id="tg-charge-1",
        payer_telegram_id=100,
    )
    await service.activate_order_by_id(order_id)

    duplicate = await service.record_successful_telegram_stars_payment(
        payload=payload,
        amount_minor_units=amount,
        currency=currency,
        telegram_payment_charge_id="tg-charge-1",
        payer_telegram_id=100,
    )

    assert duplicate.already_paid is True
    assert duplicate.subscription is not None
    assert mediator_client.rent_calls == 1


@pytest.mark.asyncio
async def test_activation_failure_can_be_retried(store: FakeStore) -> None:
    settings = make_settings()
    mediator_client = FakeMediatorClient()
    mediator_client.fail_next_rent = True
    order_id, payload, amount, currency = await create_order(store, settings, mediator_client)
    service = PurchaseService(FakeSession(store), settings, mediator_client)  # type: ignore[arg-type]

    await service.record_successful_telegram_stars_payment(
        payload=payload,
        amount_minor_units=amount,
        currency=currency,
        telegram_payment_charge_id="tg-charge-1",
        payer_telegram_id=100,
    )
    failed = await service.activate_order_by_id(order_id)

    assert failed.failure_message == "mediator_unavailable"
    assert store.orders_by_id[order_id].status == ORDER_STATUS_ACTIVATION_FAILED

    retry = await service.retry_activation_by_id(order_id)

    assert retry.activated is True
    assert retry.subscription is not None
    assert store.orders_by_id[order_id].status == ORDER_STATUS_PAID
    assert mediator_client.rent_calls == 2


@pytest.mark.asyncio
async def test_paid_order_manual_approve_is_idempotent(store: FakeStore) -> None:
    settings = make_settings(payment_mode=PAYMENT_MODE_MANUAL)
    mediator_client = FakeMediatorClient()
    order_id, _, _, _ = await create_order(store, settings, mediator_client)
    service = PurchaseService(FakeSession(store), settings, mediator_client)  # type: ignore[arg-type]

    payment = await service.prepare_manual_order_for_activation(
        order_id,
        admin_telegram_id=1,
    )

    assert payment.needs_activation is True

    await service.activate_order_by_id(order_id)
    duplicate = await service.prepare_manual_order_for_activation(
        order_id,
        admin_telegram_id=1,
    )

    assert duplicate.already_paid is True
    assert duplicate.subscription is not None
    assert mediator_client.rent_calls == 1


@pytest.mark.asyncio
async def test_pending_stars_order_cannot_be_manually_approved(store: FakeStore) -> None:
    settings = make_settings()
    mediator_client = FakeMediatorClient()
    order_id, _, _, _ = await create_order(store, settings, mediator_client)
    service = PurchaseService(FakeSession(store), settings, mediator_client)  # type: ignore[arg-type]

    with pytest.raises(ValueError, match="Only manual pending orders"):
        await service.prepare_manual_order_for_activation(order_id, admin_telegram_id=1)


@pytest.mark.asyncio
async def test_refund_eligibility_and_refunded_checkout_rejection(store: FakeStore) -> None:
    settings = make_settings()
    mediator_client = FakeMediatorClient()
    order_id, payload, amount, currency = await create_order(store, settings, mediator_client)
    service = PurchaseService(FakeSession(store), settings, mediator_client)  # type: ignore[arg-type]

    await service.record_successful_telegram_stars_payment(
        payload=payload,
        amount_minor_units=amount,
        currency=currency,
        telegram_payment_charge_id="tg-charge-1",
        payer_telegram_id=100,
    )
    await service.activate_order_by_id(order_id)

    candidate = await service.get_refund_candidate(order_id)

    assert candidate.is_eligible is True
    prepared = await service.prepare_refund(order_id)
    assert prepared.refund_operation_public_id is not None
    await service.complete_refund_after_provider(order_id)

    assert store.orders_by_id[order_id].status == ORDER_STATUS_REFUNDED
    application = store.applications_by_order_id[order_id]
    assert store.subscriptions_by_id[application.subscription_id].status == "disabled"

    is_valid, error_message = await service.validate_order_before_checkout(
        payload=payload,
        amount_minor_units=amount,
        currency=currency,
        payer_telegram_id=100,
    )

    assert is_valid is False
    assert error_message is not None

    manual_settings = make_settings(payment_mode=PAYMENT_MODE_MANUAL)
    manual_order_id, _, _, _ = await create_order(
        store,
        manual_settings,
        mediator_client,
        telegram_id=200,
    )
    manual_service = PurchaseService(  # type: ignore[arg-type]
        FakeSession(store),
        manual_settings,
        mediator_client,
    )
    await manual_service.prepare_manual_order_for_activation(manual_order_id, admin_telegram_id=1)
    await manual_service.activate_order_by_id(manual_order_id)
    manual_candidate = await manual_service.get_refund_candidate(manual_order_id)

    assert manual_candidate.is_eligible is False


def test_legacy_payment_mode_maps_to_telegram_stars() -> None:
    settings = Settings(
        TELEGRAM_BOT_TOKEN="test-token",
        PAYMENT_MODE="telegram_payments",
        MEDIATOR_ADMIN_TOKEN="test-admin-token",
    )

    assert settings.payment_mode == PAYMENT_MODE_TELEGRAM_STARS


@pytest.mark.asyncio
async def test_paid_checkout_does_not_require_document_consent(store: FakeStore) -> None:
    settings = make_settings()
    mediator_client = FakeMediatorClient()
    order_id, payload, amount, currency = await create_order(store, settings, mediator_client)
    order = store.orders_by_id[order_id]
    order.pricing_version = ProductCatalog.from_settings(settings).pricing_identity
    service = PurchaseService(FakeSession(store), settings, mediator_client)  # type: ignore[arg-type]

    valid, error_message = await service.validate_order_before_checkout(
        payload=payload,
        amount_minor_units=amount,
        currency=currency,
        payer_telegram_id=100,
        payment_bot_key=settings.default_bot_key,
    )

    assert valid is True, error_message
    assert error_message is None
    assert order.checkout_authorized_at_utc is not None


@pytest.mark.asyncio
async def test_foreign_payer_cannot_validate_or_record_payment(store: FakeStore) -> None:
    settings = make_settings()
    mediator_client = FakeMediatorClient()
    order_id, payload, amount, currency = await create_order(store, settings, mediator_client)
    service = PurchaseService(FakeSession(store), settings, mediator_client)  # type: ignore[arg-type]

    valid, error_message = await service.validate_order_before_checkout(
        payload=payload,
        amount_minor_units=amount,
        currency=currency,
        payer_telegram_id=999,
    )

    assert valid is False
    assert error_message is not None
    assert store.orders_by_id[order_id].status == ORDER_STATUS_PENDING

    with pytest.raises(ValueError, match="Order was not found"):
        await service.record_successful_telegram_stars_payment(
            payload=payload,
            amount_minor_units=amount,
            currency=currency,
            telegram_payment_charge_id="foreign-charge",
            payer_telegram_id=999,
        )

    assert store.orders_by_id[order_id].status == ORDER_STATUS_PENDING
    assert store.orders_by_id[order_id].provider_payment_id is None


@pytest.mark.asyncio
async def test_foreign_actor_cannot_claim_complimentary_order(store: FakeStore) -> None:
    settings = make_settings()
    mediator_client = FakeMediatorClient()
    owner = await FakeUserRepository(FakeSession(store)).get_or_create_from_message_user(
        telegram_id=101,
        username="owner",
        first_name="Owner",
    )
    order = Order(
        id=store.next_order_id,
        public_order_id="complimentary-owner-order",
        user_id=owner.id,
        status=ORDER_STATUS_PENDING,
        period_count=1,
        duration_days=30,
        selected_max_devices=1,
        requested_max_devices=1,
        requested_duration_days=30,
        amount_minor_units=0,
        final_amount_minor_units=0,
        price_before_personal_discount=100,
        personal_discount_id=1,
        personal_discount_bps=10_000,
        personal_discount_amount_minor_units=100,
        referral_eligible=False,
        currency=TELEGRAM_STARS_CURRENCY,
        provider=PAYMENT_MODE_TELEGRAM_STARS,
        pricing_version="test",
        order_kind="purchase",
        invoice_payload="complimentary-owner-payload",
        created_at=utc_now(),
    )
    order.user = owner
    store.next_order_id += 1
    store.orders_by_id[order.id] = order
    store.orders_by_payload[order.invoice_payload] = order
    service = PurchaseService(FakeSession(store), settings, mediator_client)  # type: ignore[arg-type]

    with pytest.raises(ValueError, match="Order was not found"):
        await service.prepare_complimentary_order_for_activation(
            order.id,
            actor_telegram_id=999,
        )

    assert order.status == ORDER_STATUS_PENDING
    assert order.provider_payment_id is None
