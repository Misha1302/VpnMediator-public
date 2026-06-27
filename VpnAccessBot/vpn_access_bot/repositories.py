from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from uuid import uuid4

from sqlalchemy import Select, case, delete, func, select, text, update
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from vpn_access_bot.constants import (
    COMMERCIAL_ENTITLEMENT_SOURCE_PAID_ORDER,
    DEVICE_UPGRADE_PRICED_ENTITLEMENT_SOURCES,
    ORDER_STATUS_ACTIVATING,
    ORDER_STATUS_ACTIVATION_FAILED,
    ORDER_STATUS_CANCELLED,
    ORDER_STATUS_EXPIRED,
    ORDER_STATUS_FAILED,
    ORDER_STATUS_PAID,
    ORDER_STATUS_PAYMENT_RECEIVED,
    ORDER_STATUS_PENDING,
    ORDER_STATUS_REFUNDED,
    PAYMENT_INBOX_APPLIED,
    PAYMENT_INBOX_MANUAL_REVIEW,
    PAYMENT_INBOX_MATCHED,
    PAYMENT_INBOX_RECEIVED,
    REFUND_OPERATION_PREPARED,
    SUBSCRIPTION_STATUS_ACTIVE,
    SUBSCRIPTION_STATUS_EXPIRED,
    TRIAL_STATUS_ACTIVATING,
    TRIAL_STATUS_ACTIVATION_FAILED,
    TRIAL_STATUS_ACTIVE,
    TRIAL_STATUS_EXPIRED,
    TRIAL_STATUS_REVOKED,
)
from vpn_access_bot.correlation import get_correlation_id
from vpn_access_bot.models import (
    AccessEntitlement,
    AccessOperationLease,
    AuditEvent,
    CommercialEntitlementAdjustment,
    CommercialEntitlementSegment,
    DeviceResetEvent,
    DiscountRedemption,
    EntitlementOperation,
    NotificationDelivery,
    NotificationOutbox,
    OnboardingSession,
    Order,
    OrderApplication,
    PaymentInbox,
    ProductEvent,
    PurchaseQuote,
    ReferralReward,
    RefundOperation,
    RefundPlan,
    Subscription,
    SupportMessage,
    SupportRequest,
    Tariff,
    TelegramBotChannel,
    TrialClaim,
    User,
    UserBotChannel,
    UserDiscount,
    utc_now,
)
from vpn_access_bot.telegram.context import get_bot_key


def to_aware_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)

    return value.astimezone(UTC)


class TelegramChannelRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def register_bot(
        self,
        *,
        bot_key: str,
        telegram_bot_id: int,
        username: str,
        enabled: bool,
        required: bool,
        verified_at_utc: datetime,
    ) -> None:
        await self._session.execute(
            sqlite_insert(TelegramBotChannel)
            .values(
                bot_key=bot_key,
                telegram_bot_id=telegram_bot_id,
                username=username,
                enabled=enabled,
                required=required,
                last_verified_at_utc=verified_at_utc,
            )
            .on_conflict_do_update(
                index_elements=[TelegramBotChannel.bot_key],
                set_={
                    "telegram_bot_id": telegram_bot_id,
                    "username": username,
                    "enabled": enabled,
                    "required": required,
                    "last_verified_at_utc": verified_at_utc,
                },
            )
        )

    async def touch_user_channel(self, user_id: int, bot_key: str) -> None:
        now = utc_now()
        await self._session.execute(
            sqlite_insert(UserBotChannel)
            .values(
                user_id=user_id,
                bot_key=bot_key,
                first_seen_at_utc=now,
                last_seen_at_utc=now,
                can_receive_messages=True,
                blocked_at_utc=None,
            )
            .on_conflict_do_update(
                index_elements=[UserBotChannel.user_id, UserBotChannel.bot_key],
                set_={
                    "last_seen_at_utc": now,
                    "can_receive_messages": True,
                    "blocked_at_utc": None,
                },
            )
        )

    async def mark_user_channel_blocked(self, user_id: int, bot_key: str) -> None:
        await self._session.execute(
            update(UserBotChannel)
            .where(
                UserBotChannel.user_id == user_id,
                UserBotChannel.bot_key == bot_key,
            )
            .values(can_receive_messages=False, blocked_at_utc=utc_now())
        )

    async def delivery_preferences_for_telegram_id(
        self, telegram_id: int
    ) -> tuple[list[str], set[str]]:
        result = await self._session.execute(
            select(
                UserBotChannel.bot_key,
                UserBotChannel.can_receive_messages,
                UserBotChannel.last_seen_at_utc,
            )
            .join(User, User.id == UserBotChannel.user_id)
            .where(User.telegram_id == telegram_id)
            .order_by(UserBotChannel.last_seen_at_utc.desc(), UserBotChannel.bot_key)
        )
        preferred: list[str] = []
        blocked: set[str] = set()
        for bot_key, can_receive_messages, _ in result.all():
            if can_receive_messages:
                preferred.append(str(bot_key))
            else:
                blocked.add(str(bot_key))
        return preferred, blocked


@dataclass(frozen=True, slots=True)
class BroadcastTarget:
    user_id: int
    telegram_id: int


class UserRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def get_or_create_from_message_user(
        self,
        telegram_id: int,
        username: str | None,
        first_name: str | None,
    ) -> User:
        now = utc_now()
        user_id: int | None = None

        for _ in range(3):
            candidate_referral_code = uuid4().hex[:18]
            statement = (
                sqlite_insert(User)
                .values(
                    telegram_id=telegram_id,
                    username=username,
                    first_name=first_name,
                    referral_code=candidate_referral_code,
                    created_at=now,
                    updated_at=now,
                )
                .on_conflict_do_update(
                    index_elements=[User.telegram_id],
                    set_={
                        "username": username,
                        "first_name": first_name,
                        "updated_at": now,
                    },
                )
                .returning(User.id)
            )
            try:
                async with self._session.begin_nested():
                    result = await self._session.execute(statement)
                    user_id = int(result.scalar_one())
                break
            except IntegrityError:
                continue

        if user_id is None:
            raise RuntimeError("telegram_user_upsert_failed")

        user = await self.get_by_id(user_id)
        if user is None:
            raise RuntimeError("telegram_user_upsert_not_visible")
        bot_key = get_bot_key()
        if bot_key is not None:
            await TelegramChannelRepository(self._session).touch_user_channel(user.id, bot_key)
        return user

    async def get_by_telegram_id(self, telegram_id: int) -> User | None:
        result = await self._session.execute(select(User).where(User.telegram_id == telegram_id))
        return result.scalar_one_or_none()

    async def get_by_id(self, user_id: int) -> User | None:
        result = await self._session.execute(select(User).where(User.id == user_id))
        return result.scalar_one_or_none()

    async def list_broadcast_targets(self) -> list[BroadcastTarget]:
        result = await self._session.execute(
            select(User.id, User.telegram_id).order_by(User.telegram_id, User.id)
        )
        return [
            BroadcastTarget(user_id=int(user_id), telegram_id=int(telegram_id))
            for user_id, telegram_id in result.all()
        ]


class TariffRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def list_active(self) -> list[Tariff]:
        result = await self._session.execute(
            select(Tariff).where(Tariff.is_active.is_(True)).order_by(Tariff.sort_order, Tariff.id),
        )
        return list(result.scalars().all())

    async def get_active_by_code(self, code: str) -> Tariff | None:
        result = await self._session.execute(
            select(Tariff).where(Tariff.code == code, Tariff.is_active.is_(True)),
        )
        return result.scalar_one_or_none()


class OrderRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def get_latest_for_user(self, user_id: int) -> Order | None:
        result = await self._session.execute(
            self._base_query()
            .where(Order.user_id == user_id)
            .order_by(Order.created_at.desc(), Order.id.desc())
            .limit(1),
        )
        return result.scalar_one_or_none()

    async def has_unfinished_for_subscription(self, subscription_id: int) -> bool:
        result = await self._session.execute(
            select(func.count(Order.id)).where(
                Order.target_subscription_id == subscription_id,
                Order.status.in_(
                    [
                        ORDER_STATUS_PAYMENT_RECEIVED,
                        ORDER_STATUS_ACTIVATING,
                        ORDER_STATUS_ACTIVATION_FAILED,
                    ]
                ),
            )
        )
        return int(result.scalar_one()) > 0

    async def get_relevant_unfinished_for_user(self, user_id: int) -> Order | None:
        priority = case(
            (Order.status == ORDER_STATUS_ACTIVATION_FAILED, 0),
            (Order.status == ORDER_STATUS_PAYMENT_RECEIVED, 1),
            (Order.status == ORDER_STATUS_ACTIVATING, 2),
            (Order.status == ORDER_STATUS_PENDING, 3),
            else_=99,
        )
        result = await self._session.execute(
            self._base_query()
            .where(
                Order.user_id == user_id,
                Order.status.in_(
                    [
                        ORDER_STATUS_PENDING,
                        ORDER_STATUS_PAYMENT_RECEIVED,
                        ORDER_STATUS_ACTIVATING,
                        ORDER_STATUS_ACTIVATION_FAILED,
                    ]
                ),
            )
            .order_by(priority, Order.created_at.desc(), Order.id.desc())
            .limit(1),
        )
        return result.scalar_one_or_none()

    async def get_by_public_id_for_user(
        self,
        public_order_id: str,
        user_id: int,
    ) -> Order | None:
        result = await self._session.execute(
            self._base_query().where(
                Order.public_order_id == public_order_id,
                Order.user_id == user_id,
            )
        )
        return result.scalar_one_or_none()

    async def get_by_public_id(self, public_order_id: str) -> Order | None:
        result = await self._session.execute(
            self._base_query().where(Order.public_order_id == public_order_id)
        )
        return result.scalar_one_or_none()

    async def create_pending_order(
        self,
        user: User,
        tariff: Tariff,
        provider: str,
        amount_minor_units: int | None = None,
        currency: str | None = None,
    ) -> Order:
        order = Order(
            user=user,
            origin_bot_key=get_bot_key(),
            tariff=tariff,
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
            invoice_payload=f"order:{uuid4().hex}",
            created_at=utc_now(),
        )
        self._session.add(order)
        await self._session.flush()
        return order

    async def create_order_from_quote(
        self,
        quote: PurchaseQuote,
        provider: str,
        invoice_payload: str,
        expires_at: datetime,
        *,
        amount_minor_units: int | None = None,
        currency: str | None = None,
        pricing_version: str | None = None,
        upgrade_amount_minor_units: int | None = None,
        extension_amount_minor_units: int | None = None,
        price_before_personal_discount: int | None = None,
        personal_discount_amount_minor_units: int | None = None,
        base_expires_at_utc: datetime,
        purchased_duration_days: int,
        expiration_policy_version: str,
        target_expires_at_utc: datetime,
    ) -> Order:
        order = Order(
            user_id=quote.user_id,
            origin_bot_key=quote.origin_bot_key or get_bot_key(),
            quote_id=quote.id,
            status=ORDER_STATUS_PENDING,
            period_count=quote.period_count,
            duration_days=quote.duration_days,
            selected_max_devices=quote.max_devices,
            amount_minor_units=(
                quote.amount_minor_units if amount_minor_units is None else amount_minor_units
            ),
            currency=(currency or quote.currency).upper(),
            provider=provider,
            pricing_version=pricing_version or quote.pricing_version,
            target_subscription_id=quote.target_subscription_id,
            order_kind=quote.order_kind,
            base_entitlement_version=quote.base_entitlement_version,
            base_valid_until_utc=quote.base_valid_until_utc,
            base_max_devices=quote.base_max_devices,
            requested_max_devices=quote.requested_max_devices,
            requested_duration_days=quote.requested_duration_days,
            upgrade_amount_minor_units=(
                quote.upgrade_amount_minor_units
                if upgrade_amount_minor_units is None
                else upgrade_amount_minor_units
            ),
            extension_amount_minor_units=(
                quote.extension_amount_minor_units
                if extension_amount_minor_units is None
                else extension_amount_minor_units
            ),
            price_before_personal_discount=(
                quote.price_before_personal_discount
                if price_before_personal_discount is None
                else price_before_personal_discount
            ),
            personal_discount_id=quote.personal_discount_id,
            personal_discount_bps=quote.personal_discount_bps,
            personal_discount_amount_minor_units=(
                quote.personal_discount_amount_minor_units
                if personal_discount_amount_minor_units is None
                else personal_discount_amount_minor_units
            ),
            final_amount_minor_units=(
                quote.amount_minor_units if amount_minor_units is None else amount_minor_units
            ),
            referral_eligible=quote.referral_eligible,
            is_test_order=quote.is_test_order,
            trial_claim_id=quote.trial_claim_id,
            trial_seconds_remaining_at_quote=quote.trial_seconds_remaining_at_quote,
            invoice_payload=invoice_payload,
            created_at=utc_now(),
            expires_at_utc=expires_at,
            base_expires_at_utc=base_expires_at_utc,
            purchased_duration_days=purchased_duration_days,
            expiration_policy_version=expiration_policy_version,
            target_expires_at_utc=target_expires_at_utc,
        )
        self._session.add(order)
        await self._session.flush()
        return order

    async def get_for_payment_payload(self, payload: str) -> Order | None:
        result = await self._session.execute(
            self._base_query().where(Order.invoice_payload == payload)
        )
        return result.scalar_one_or_none()

    async def get_for_payment_payload_for_user(
        self,
        payload: str,
        user_id: int,
    ) -> Order | None:
        result = await self._session.execute(
            self._base_query().where(
                Order.invoice_payload == payload,
                Order.user_id == user_id,
            )
        )
        return result.scalar_one_or_none()

    async def get_unique_for_payment_payload_hash_for_user(
        self,
        payload_hash: str,
        user_id: int,
    ) -> Order | None:
        result = await self._session.execute(
            select(Order.id, Order.invoice_payload).where(Order.user_id == user_id)
        )
        matching_ids = [
            int(order_id)
            for order_id, invoice_payload in result.all()
            if hashlib.sha256(invoice_payload.encode("utf-8")).hexdigest() == payload_hash
        ]
        if len(matching_ids) != 1:
            return None
        return await self.get_by_id(matching_ids[0])

    async def get_by_id_for_user(self, order_id: int, user_id: int) -> Order | None:
        result = await self._session.execute(
            self._base_query().where(Order.id == order_id, Order.user_id == user_id)
        )
        return result.scalar_one_or_none()

    async def get_by_id(self, order_id: int) -> Order | None:
        result = await self._session.execute(self._base_query().where(Order.id == order_id))
        return result.scalar_one_or_none()

    async def get_for_quote(self, quote_id: int) -> Order | None:
        result = await self._session.execute(self._base_query().where(Order.quote_id == quote_id))
        return result.scalar_one_or_none()

    async def attach_provider_payment(
        self,
        order: Order,
        *,
        provider_payment_id: str,
        provider_payment_status: str,
        confirmation_url: str | None,
    ) -> None:
        if order.provider_payment_id not in {None, provider_payment_id}:
            raise ValueError("provider_payment_conflict")
        order.provider_payment_id = provider_payment_id
        order.provider_payment_status = provider_payment_status
        order.provider_confirmation_url = confirmation_url
        await self._session.flush()

    async def update_provider_payment_status(self, order: Order, status: str) -> None:
        order.provider_payment_status = status
        if status == "canceled" and order.status == ORDER_STATUS_PENDING:
            order.status = ORDER_STATUS_CANCELLED
            order.cancelled_at_utc = utc_now()
        await self._session.flush()

    async def claim_payment_bot(self, order: Order, bot_key: str) -> bool:
        normalized = bot_key.strip().lower()
        if not normalized:
            return False
        result = await self._session.execute(
            update(Order)
            .where(
                Order.id == order.id,
                (Order.payment_bot_key.is_(None) | (Order.payment_bot_key == normalized)),
            )
            .values(payment_bot_key=normalized)
            .execution_options(synchronize_session=False)
        )
        if result.rowcount != 1:
            return False
        order.payment_bot_key = normalized
        return True

    async def get_by_provider_payment_id(
        self,
        provider: str,
        provider_payment_id: str,
    ) -> Order | None:
        result = await self._session.execute(
            self._base_query().where(
                Order.provider == provider,
                Order.provider_payment_id == provider_payment_id,
            )
        )
        return result.scalar_one_or_none()

    async def list_by_status(self, status: str, limit: int = 20) -> list[Order]:
        result = await self._session.execute(
            self._base_query()
            .where(Order.status == status)
            .order_by(Order.created_at.desc(), Order.id.desc())
            .limit(limit),
        )
        return list(result.scalars().all())

    async def list_legacy_activating_without_operation(
        self,
        limit: int = 100,
    ) -> list[Order]:
        operation_exists = (
            select(EntitlementOperation.id)
            .where(
                EntitlementOperation.source_entity_type == "order",
                EntitlementOperation.source_entity_id == Order.public_order_id,
                EntitlementOperation.operation_type.in_(["paid_activation", "complimentary"]),
            )
            .correlate(Order)
            .exists()
        )
        result = await self._session.execute(
            self._base_query()
            .where(
                Order.status == ORDER_STATUS_ACTIVATING,
                ~operation_exists,
            )
            .order_by(Order.created_at, Order.id)
            .limit(limit)
        )
        return list(result.scalars().all())

    async def get_recent_for_user_by_statuses(
        self,
        user_id: int,
        statuses: list[str],
        limit: int = 1,
    ) -> list[Order]:
        result = await self._session.execute(
            self._base_query()
            .where(Order.user_id == user_id, Order.status.in_(statuses))
            .order_by(Order.created_at.desc(), Order.id.desc())
            .limit(limit),
        )
        return list(result.scalars().all())

    async def mark_payment_received(
        self,
        order: Order,
        provider_payment_id: str,
        paid_at: datetime | None = None,
    ) -> None:
        order.status = ORDER_STATUS_PAYMENT_RECEIVED
        order.provider_payment_id = provider_payment_id
        order.paid_at = paid_at or utc_now()

    async def try_mark_checkout_authorized(
        self,
        order: Order,
        *,
        authorized_at_utc: datetime,
        authorized_until_utc: datetime,
    ) -> bool:
        result = await self._session.execute(
            update(Order)
            .where(
                Order.id == order.id,
                Order.status == ORDER_STATUS_PENDING,
                (Order.expires_at_utc.is_(None) | (Order.expires_at_utc > authorized_at_utc)),
            )
            .values(
                checkout_authorized_at_utc=authorized_at_utc,
                checkout_authorized_until_utc=authorized_until_utc,
            )
            .execution_options(synchronize_session=False)
        )
        if result.rowcount != 1:
            return False
        order.checkout_authorized_at_utc = authorized_at_utc
        order.checkout_authorized_until_utc = authorized_until_utc
        return True

    async def list_activation_candidates(
        self,
        *,
        now: datetime,
        limit: int = 20,
    ) -> list[Order]:
        result = await self._session.execute(
            self._base_query()
            .where(
                (Order.status == ORDER_STATUS_PAYMENT_RECEIVED)
                | (
                    (Order.status == ORDER_STATUS_ACTIVATION_FAILED)
                    & (
                        Order.next_activation_retry_at_utc.is_(None)
                        | (Order.next_activation_retry_at_utc <= now)
                    )
                )
            )
            .order_by(Order.paid_at, Order.created_at, Order.id)
            .limit(limit)
        )
        return list(result.scalars().all())

    async def mark_complimentary_ready(self, order: Order) -> None:
        order.status = ORDER_STATUS_PAYMENT_RECEIVED
        order.provider_payment_id = None
        order.paid_at = None

    async def mark_activating(self, order: Order) -> None:
        order.status = ORDER_STATUS_ACTIVATING
        order.activation_attempt_count += 1
        order.last_activation_attempt_at_utc = utc_now()
        order.last_activation_error_code = None
        order.next_activation_retry_at_utc = None

    async def mark_paid(self, order: Order, provider_payment_id: str | None = None) -> None:
        order.status = ORDER_STATUS_PAID
        order.completed_at_utc = utc_now()
        order.last_activation_error_code = None
        order.next_activation_retry_at_utc = None

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
        order.last_activation_error_code = error_code[:64]
        order.next_activation_retry_at_utc = utc_now() + timedelta(minutes=1)

    async def mark_failed(self, order: Order) -> None:
        order.status = ORDER_STATUS_FAILED

    async def mark_refunded(self, order: Order) -> None:
        order.status = ORDER_STATUS_REFUNDED

    async def mark_expired(self, order: Order) -> None:
        order.status = ORDER_STATUS_EXPIRED
        order.cancelled_at_utc = utc_now()

    async def transition_status(
        self,
        order_id: int,
        expected_statuses: list[str],
        next_status: str,
    ) -> bool:
        values: dict[str, object] = {"status": next_status}
        if next_status == ORDER_STATUS_ACTIVATING:
            values.update(
                {
                    "activation_attempt_count": Order.activation_attempt_count + 1,
                    "last_activation_attempt_at_utc": utc_now(),
                    "last_activation_error_code": None,
                    "next_activation_retry_at_utc": None,
                }
            )
        result = await self._session.execute(
            update(Order)
            .where(Order.id == order_id, Order.status.in_(expected_statuses))
            .values(**values)
        )
        return result.rowcount == 1

    def _base_query(self) -> Select[tuple[Order]]:
        return select(Order).options(selectinload(Order.user), selectinload(Order.tariff))


class SubscriptionRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def get_latest_for_user(self, user_id: int) -> Subscription | None:
        result = await self._session.execute(
            self._base_query()
            .where(
                Subscription.user_id == user_id,
                Subscription.test_reset_at_utc.is_(None),
            )
            .order_by(Subscription.created_at.desc(), Subscription.id.desc())
            .limit(1),
        )
        return result.scalar_one_or_none()

    async def get_primary_for_user(self, user: User) -> Subscription | None:
        if user.primary_subscription_id is not None:
            primary = await self.get_by_id(user.primary_subscription_id)

            if primary is not None and primary.user_id == user.id:
                return primary

        latest = await self.get_latest_for_user(user.id)

        if latest is not None and user.primary_subscription_id != latest.id:
            user.primary_subscription_id = latest.id
            user.updated_at = utc_now()

        return latest

    async def get_active_for_user(
        self, user_id: int, now: datetime | None = None
    ) -> Subscription | None:
        now = to_aware_utc(now or utc_now())
        result = await self._session.execute(
            self._base_query()
            .where(
                Subscription.user_id == user_id,
                Subscription.test_reset_at_utc.is_(None),
                Subscription.status == SUBSCRIPTION_STATUS_ACTIVE,
                Subscription.expires_at > now,
            )
            .order_by(Subscription.expires_at.desc(), Subscription.id.desc())
            .limit(1),
        )
        return result.scalar_one_or_none()

    async def list_visible_for_user(self, user_id: int) -> list[Subscription]:
        result = await self._session.execute(
            self._base_query()
            .where(
                Subscription.user_id == user_id,
                Subscription.test_reset_at_utc.is_(None),
            )
            .order_by(Subscription.created_at, Subscription.id)
        )
        return list(result.scalars().all())

    async def get_by_public_guid(self, public_guid: str) -> Subscription | None:
        result = await self._session.execute(
            self._base_query().where(Subscription.public_guid == public_guid),
        )
        return result.scalar_one_or_none()

    async def get_by_id(self, subscription_id: int) -> Subscription | None:
        result = await self._session.execute(
            self._base_query().where(Subscription.id == subscription_id),
        )
        return result.scalar_one_or_none()

    async def create(
        self,
        user: User,
        tariff: Tariff | None,
        public_guid: str,
        expires_at: datetime,
        max_devices: int | None = None,
    ) -> Subscription:
        now = utc_now()
        subscription = Subscription(
            user=user,
            tariff=tariff,
            public_guid=public_guid,
            signed_url="",
            max_devices=(
                max_devices if max_devices is not None else (tariff.max_devices if tariff else 1)
            ),
            status=SUBSCRIPTION_STATUS_ACTIVE,
            starts_at=now,
            expires_at=to_aware_utc(expires_at),
            created_at=now,
            updated_at_utc=now,
        )
        self._session.add(subscription)
        await self._session.flush()
        user.primary_subscription_id = subscription.id
        user.updated_at = now
        return subscription

    async def extend(
        self,
        subscription: Subscription,
        tariff: Tariff | None,
        new_expires_at: datetime,
        max_devices: int | None = None,
    ) -> None:
        subscription.tariff_id = tariff.id if tariff is not None else subscription.tariff_id
        subscription.max_devices = (
            max_devices
            if max_devices is not None
            else (tariff.max_devices if tariff else subscription.max_devices)
        )
        subscription.status = SUBSCRIPTION_STATUS_ACTIVE
        subscription.expires_at = to_aware_utc(new_expires_at)
        subscription.updated_at_utc = utc_now()
        subscription.disabled_at = None

    async def mark_expired(self, subscription: Subscription) -> None:
        subscription.status = SUBSCRIPTION_STATUS_EXPIRED
        subscription.updated_at_utc = utc_now()
        subscription.disabled_at = None

    async def list_expired_active(
        self, now: datetime | None = None, limit: int = 50
    ) -> list[Subscription]:
        now = to_aware_utc(now or utc_now())
        result = await self._session.execute(
            self._base_query()
            .where(
                Subscription.status == SUBSCRIPTION_STATUS_ACTIVE,
                Subscription.test_reset_at_utc.is_(None),
                Subscription.expires_at <= now,
            )
            .order_by(Subscription.expires_at.asc())
            .limit(limit),
        )
        return list(result.scalars().all())

    async def count_active(self, now: datetime | None = None) -> int:
        now = to_aware_utc(now or utc_now())
        result = await self._session.execute(
            select(func.count(Subscription.id)).where(
                Subscription.status == SUBSCRIPTION_STATUS_ACTIVE,
                Subscription.test_reset_at_utc.is_(None),
                Subscription.expires_at > now,
            ),
        )
        return int(result.scalar_one())

    def _base_query(self) -> Select[tuple[Subscription]]:
        return select(Subscription).options(
            selectinload(Subscription.user),
            selectinload(Subscription.tariff),
        )


class DeviceResetRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def get_last_reset(self, subscription_id: int) -> DeviceResetEvent | None:
        result = await self._session.execute(
            select(DeviceResetEvent)
            .where(DeviceResetEvent.subscription_id == subscription_id)
            .order_by(DeviceResetEvent.created_at.desc(), DeviceResetEvent.id.desc())
            .limit(1),
        )
        return result.scalar_one_or_none()

    async def can_reset(
        self, subscription_id: int, cooldown_hours: int
    ) -> tuple[bool, datetime | None]:
        last_reset = await self.get_last_reset(subscription_id)

        if last_reset is None:
            return True, None

        next_allowed_at = to_aware_utc(last_reset.created_at) + timedelta(hours=cooldown_hours)

        if next_allowed_at <= utc_now():
            return True, None

        return False, next_allowed_at

    async def add_reset_event(self, subscription: Subscription, user: User) -> DeviceResetEvent:
        event = DeviceResetEvent(
            subscription_id=subscription.id,
            user_id=user.id,
            created_at=utc_now(),
        )
        self._session.add(event)
        await self._session.flush()
        return event


class PurchaseQuoteRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def create(
        self,
        user: User,
        period_count: int,
        duration_days: int,
        max_devices: int,
        amount_minor_units: int,
        currency: str,
        pricing_version: str,
        target_subscription_id: int | None,
        order_kind: str,
        expires_at: datetime,
        base_entitlement_version: int | None = None,
        base_valid_until_utc: datetime | None = None,
        base_max_devices: int | None = None,
        upgrade_amount_minor_units: int = 0,
        extension_amount_minor_units: int | None = None,
        personal_discount_id: int | None = None,
        personal_discount_bps: int = 0,
        personal_discount_amount_minor_units: int = 0,
        referral_eligible: bool = True,
        is_test_order: bool = False,
        trial_claim_id: int | None = None,
        trial_seconds_remaining_at_quote: int = 0,
        remaining_paid_seconds_at_quote: int = 0,
    ) -> PurchaseQuote:
        quote = PurchaseQuote(
            user_id=user.id,
            origin_bot_key=get_bot_key(),
            period_count=period_count,
            duration_days=duration_days,
            max_devices=max_devices,
            amount_minor_units=amount_minor_units,
            currency=currency.upper(),
            pricing_version=pricing_version,
            target_subscription_id=target_subscription_id,
            order_kind=order_kind,
            base_entitlement_version=base_entitlement_version,
            base_valid_until_utc=(
                to_aware_utc(base_valid_until_utc) if base_valid_until_utc is not None else None
            ),
            base_max_devices=base_max_devices,
            requested_max_devices=max_devices,
            requested_duration_days=duration_days,
            upgrade_amount_minor_units=upgrade_amount_minor_units,
            extension_amount_minor_units=(
                extension_amount_minor_units
                if extension_amount_minor_units is not None
                else amount_minor_units
            ),
            price_before_personal_discount=amount_minor_units
            + personal_discount_amount_minor_units,
            personal_discount_id=personal_discount_id,
            personal_discount_bps=personal_discount_bps,
            personal_discount_amount_minor_units=personal_discount_amount_minor_units,
            final_amount_minor_units=amount_minor_units,
            referral_eligible=referral_eligible,
            is_test_order=is_test_order,
            trial_claim_id=trial_claim_id,
            trial_seconds_remaining_at_quote=trial_seconds_remaining_at_quote,
            remaining_paid_seconds_at_quote=remaining_paid_seconds_at_quote,
            expires_at_utc=to_aware_utc(expires_at),
            created_at_utc=utc_now(),
        )
        self._session.add(quote)
        await self._session.flush()
        return quote

    async def get_by_public_id_for_user(
        self,
        public_quote_id: str,
        user_id: int,
    ) -> PurchaseQuote | None:
        result = await self._session.execute(
            select(PurchaseQuote).where(
                PurchaseQuote.public_quote_id == public_quote_id,
                PurchaseQuote.user_id == user_id,
            )
        )
        return result.scalar_one_or_none()

    async def get_by_public_id(self, public_quote_id: str) -> PurchaseQuote | None:
        result = await self._session.execute(
            select(PurchaseQuote)
            .options(selectinload(PurchaseQuote.user))
            .where(PurchaseQuote.public_quote_id == public_quote_id)
        )
        return result.scalar_one_or_none()

    async def try_consume(self, quote_id: int, consumed_at_utc: datetime) -> bool:
        result = await self._session.execute(
            update(PurchaseQuote)
            .where(
                PurchaseQuote.id == quote_id,
                PurchaseQuote.consumed_at_utc.is_(None),
            )
            .values(consumed_at_utc=to_aware_utc(consumed_at_utc))
            .execution_options(synchronize_session=False)
        )
        return result.rowcount == 1


class EntitlementRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def get_for_subscription(self, subscription_id: int) -> AccessEntitlement | None:
        result = await self._session.execute(
            select(AccessEntitlement).where(AccessEntitlement.subscription_id == subscription_id)
        )
        return result.scalar_one_or_none()

    async def upsert(
        self,
        subscription: Subscription,
        status: str,
        valid_until_utc: datetime,
        max_device_tokens: int,
    ) -> AccessEntitlement:
        existing = await self.get_for_subscription(subscription.id)
        now = utc_now()

        if existing is None:
            entitlement = AccessEntitlement(
                subscription_id=subscription.id,
                version=1,
                status=status,
                valid_until_utc=to_aware_utc(valid_until_utc),
                max_device_tokens=max_device_tokens,
                updated_at_utc=now,
            )
            self._session.add(entitlement)
            await self._session.flush()
            return entitlement

        existing.version += 1
        existing.status = status
        existing.valid_until_utc = to_aware_utc(valid_until_utc)
        existing.max_device_tokens = max_device_tokens
        existing.updated_at_utc = now
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
        now = utc_now()
        if existing is None:
            existing = AccessEntitlement(
                subscription_id=subscription.id,
                version=version,
                status=status,
                valid_until_utc=to_aware_utc(valid_until_utc),
                max_device_tokens=max_device_tokens,
                updated_at_utc=now,
            )
            self._session.add(existing)
            await self._session.flush()
            return existing
        existing.version = version
        existing.status = status
        existing.valid_until_utc = to_aware_utc(valid_until_utc)
        existing.max_device_tokens = max_device_tokens
        existing.updated_at_utc = now
        return existing


class OrderApplicationRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def get_for_order(self, order_id: int) -> OrderApplication | None:
        result = await self._session.execute(
            select(OrderApplication).where(OrderApplication.order_id == order_id)
        )
        return result.scalar_one_or_none()

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
            order_id=order.id,
            subscription_id=subscription.id,
            applied_at_utc=utc_now(),
            duration_days=order.duration_days,
            selected_max_devices=order.selected_max_devices,
            resulting_valid_until_utc=to_aware_utc(resulting_valid_until_utc),
            resulting_entitlement_version=resulting_entitlement_version,
            previous_entitlement_version=previous_entitlement_version,
            previous_status=previous_status,
            previous_valid_until_utc=(
                to_aware_utc(previous_valid_until_utc)
                if previous_valid_until_utc is not None
                else None
            ),
            previous_max_devices=previous_max_devices,
        )
        self._session.add(application)
        await self._session.flush()
        return application

    async def has_later_application(
        self,
        subscription_id: int,
        applied_after_utc: datetime,
        exclude_order_id: int,
    ) -> bool:
        result = await self._session.execute(
            select(func.count(OrderApplication.id)).where(
                OrderApplication.subscription_id == subscription_id,
                OrderApplication.order_id != exclude_order_id,
                OrderApplication.applied_at_utc > to_aware_utc(applied_after_utc),
            )
        )
        return int(result.scalar_one()) > 0


class CommercialEntitlementAdjustmentRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

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
        existing = await self._session.execute(
            select(CommercialEntitlementAdjustment).where(
                CommercialEntitlementAdjustment.idempotency_key == idempotency_key
            )
        )
        adjustment = existing.scalar_one_or_none()

        if adjustment is not None:
            return adjustment

        now = utc_now()
        adjustment = CommercialEntitlementAdjustment(
            subscription_id=subscription.id,
            source_kind=source_kind,
            duration_delta_seconds=duration_delta_seconds,
            device_limit_before=device_limit_before,
            device_limit_after=device_limit_after,
            source_order_id=source_order_id,
            source_entity_id=source_entity_id,
            idempotency_key=idempotency_key,
            status="applied",
            created_at_utc=now,
            applied_at_utc=now,
        )
        self._session.add(adjustment)
        await self._session.flush()
        return adjustment

    async def mark_reversed_for_source_entity(self, source_kind: str, source_entity_id: str) -> int:
        result = await self._session.execute(
            update(CommercialEntitlementAdjustment)
            .where(
                CommercialEntitlementAdjustment.source_kind == source_kind,
                CommercialEntitlementAdjustment.source_entity_id == source_entity_id,
                CommercialEntitlementAdjustment.status == "applied",
            )
            .values(status="reversed")
        )
        return int(result.rowcount or 0)

    async def mark_reversed_for_order(self, order_id: int) -> int:
        result = await self._session.execute(
            update(CommercialEntitlementAdjustment)
            .where(
                CommercialEntitlementAdjustment.source_order_id == order_id,
                CommercialEntitlementAdjustment.status == "applied",
            )
            .values(status="reversed")
        )
        return int(result.rowcount or 0)


class CommercialEntitlementSegmentRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

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
        result = await self._session.execute(
            select(CommercialEntitlementSegment).where(
                CommercialEntitlementSegment.idempotency_key == idempotency_key
            )
        )
        existing = result.scalar_one_or_none()

        if existing is not None:
            return existing

        segment = CommercialEntitlementSegment(
            subscription_id=subscription_id,
            source_kind=source_kind,
            starts_at_utc=to_aware_utc(starts_at_utc),
            ends_at_utc=to_aware_utc(ends_at_utc),
            source_order_id=source_order_id,
            source_entity_id=source_entity_id,
            idempotency_key=idempotency_key,
            status="applied",
            created_at_utc=utc_now(),
        )
        self._session.add(segment)
        await self._session.flush()
        return segment

    async def remaining_paid_seconds(
        self,
        subscription: Subscription,
        now: datetime | None = None,
    ) -> int:
        now = to_aware_utc(now or utc_now())
        segments = await self._active_segments(
            subscription_id=subscription.id,
            source_kinds=frozenset({COMMERCIAL_ENTITLEMENT_SOURCE_PAID_ORDER}),
            now=now,
        )

        if segments:
            return self._remaining_seconds(segments, now)

        application_count = await self._session.execute(
            select(func.count(OrderApplication.id)).where(
                OrderApplication.subscription_id == subscription.id
            )
        )

        if int(application_count.scalar_one()) == 0:
            return max(
                int((to_aware_utc(subscription.expires_at) - now).total_seconds()),
                0,
            )

        raise RuntimeError("paid_entitlement_segments_missing")

    async def remaining_device_upgrade_seconds(
        self,
        subscription: Subscription,
        now: datetime | None = None,
    ) -> int:
        now = to_aware_utc(now or utc_now())
        active_segments = await self._active_segments(
            subscription_id=subscription.id,
            source_kinds=DEVICE_UPGRADE_PRICED_ENTITLEMENT_SOURCES,
            now=now,
        )

        if active_segments:
            return self._remaining_seconds(active_segments, now)

        any_segment_count = await self._session.execute(
            select(func.count(CommercialEntitlementSegment.id)).where(
                CommercialEntitlementSegment.subscription_id == subscription.id,
                CommercialEntitlementSegment.source_kind.in_(
                    DEVICE_UPGRADE_PRICED_ENTITLEMENT_SOURCES
                ),
            )
        )
        if int(any_segment_count.scalar_one()) > 0:
            return 0

        commercial_application_count = await self._session.execute(
            select(func.count(OrderApplication.id))
            .join(Order, Order.id == OrderApplication.order_id)
            .where(
                OrderApplication.subscription_id == subscription.id,
                OrderApplication.duration_days > 0,
                Order.is_test_order.is_(False),
                Order.status == ORDER_STATUS_PAID,
            )
        )
        if int(commercial_application_count.scalar_one()) > 0:
            raise RuntimeError("device_upgrade_entitlement_segments_missing")

        return 0

    async def _active_segments(
        self,
        *,
        subscription_id: int,
        source_kinds: frozenset[str],
        now: datetime,
    ) -> list[CommercialEntitlementSegment]:
        result = await self._session.execute(
            select(CommercialEntitlementSegment).where(
                CommercialEntitlementSegment.subscription_id == subscription_id,
                CommercialEntitlementSegment.source_kind.in_(source_kinds),
                CommercialEntitlementSegment.status == "applied",
                CommercialEntitlementSegment.ends_at_utc > now,
            )
        )
        return list(result.scalars().all())

    @staticmethod
    def _remaining_seconds(
        segments: list[CommercialEntitlementSegment],
        now: datetime,
    ) -> int:
        return sum(
            max(
                int(
                    (
                        to_aware_utc(segment.ends_at_utc)
                        - max(to_aware_utc(segment.starts_at_utc), now)
                    ).total_seconds()
                ),
                0,
            )
            for segment in segments
        )

    async def remaining_seconds_for_source_entity(
        self,
        source_kind: str,
        source_entity_id: str,
        now: datetime | None = None,
    ) -> int:
        now = to_aware_utc(now or utc_now())
        result = await self._session.execute(
            select(CommercialEntitlementSegment).where(
                CommercialEntitlementSegment.source_kind == source_kind,
                CommercialEntitlementSegment.source_entity_id == source_entity_id,
                CommercialEntitlementSegment.status == "applied",
            )
        )
        segments = list(result.scalars().all())
        return self._remaining_seconds(segments, now)

    async def mark_reversed_for_source_entity(self, source_kind: str, source_entity_id: str) -> int:
        now = utc_now()
        result = await self._session.execute(
            update(CommercialEntitlementSegment)
            .where(
                CommercialEntitlementSegment.source_kind == source_kind,
                CommercialEntitlementSegment.source_entity_id == source_entity_id,
                CommercialEntitlementSegment.status == "applied",
            )
            .values(status="reversed", reversed_at_utc=now)
        )
        return int(result.rowcount or 0)

    async def mark_reversed_for_order(self, order_id: int) -> int:
        now = utc_now()
        result = await self._session.execute(
            update(CommercialEntitlementSegment)
            .where(
                CommercialEntitlementSegment.source_order_id == order_id,
                CommercialEntitlementSegment.status == "applied",
            )
            .values(status="reversed", reversed_at_utc=now)
        )
        return int(result.rowcount or 0)


class TrialClaimRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def get_for_user(self, user_id: int) -> TrialClaim | None:
        result = await self._session.execute(
            select(TrialClaim).where(TrialClaim.user_id == user_id)
        )
        return result.scalar_one_or_none()

    async def mark_terminal_for_subscription(
        self,
        subscription_id: int,
        *,
        terminal_status: str,
        occurred_at_utc: datetime,
    ) -> bool:
        if terminal_status not in {TRIAL_STATUS_EXPIRED, TRIAL_STATUS_REVOKED}:
            raise ValueError("trial_terminal_status_invalid")

        timestamp_column = (
            TrialClaim.expired_at_utc
            if terminal_status == TRIAL_STATUS_EXPIRED
            else TrialClaim.revoked_at_utc
        )
        result = await self._session.execute(
            update(TrialClaim)
            .where(
                TrialClaim.subscription_id == subscription_id,
                TrialClaim.status.in_(
                    (
                        TRIAL_STATUS_ACTIVATING,
                        TRIAL_STATUS_ACTIVATION_FAILED,
                        TRIAL_STATUS_ACTIVE,
                    )
                ),
            )
            .values(
                status=terminal_status,
                **{timestamp_column.key: occurred_at_utc},
            )
        )
        return bool(result.rowcount)

    async def acquire_activation(
        self,
        user: User,
        duration_seconds: int,
        max_devices: int,
    ) -> tuple[TrialClaim | None, bool]:
        now = utc_now()
        reset_generation = max(int(user.test_user_reset_generation), 0)
        idempotency_key = (
            f"trial:{user.id}"
            if reset_generation == 0
            else f"trial:{user.id}:reset:{reset_generation}"
        )
        paid_history_cutoff_utc = user.test_user_reset_at_utc
        insert_result = await self._session.execute(
            text(
                """
                INSERT INTO trial_claims (
                    user_id, status, duration_seconds, max_devices,
                    idempotency_key, created_at_utc, reserved_at_utc
                )
                SELECT
                    :user_id, 'activating', :duration_seconds, :max_devices,
                    :idempotency_key, :created_at_utc, :created_at_utc
                WHERE NOT EXISTS (
                    SELECT 1 FROM trial_claims WHERE user_id = :user_id
                )
                AND NOT EXISTS (
                    SELECT 1
                    FROM orders
                    WHERE user_id = :user_id
                      AND is_test_order = 0
                      AND amount_minor_units > 0
                      AND (
                          paid_at IS NOT NULL
                          OR provider_payment_id IS NOT NULL
                          OR status = 'paid'
                      )
                      AND (
                          :paid_history_cutoff_utc IS NULL
                          OR COALESCE(paid_at, created_at) > :paid_history_cutoff_utc
                      )
                )
                AND NOT EXISTS (
                    SELECT 1
                    FROM subscriptions
                    WHERE user_id = :user_id
                      AND status = 'active'
                      AND expires_at > :created_at_utc
                )
                ON CONFLICT(user_id) DO NOTHING
                """
            ),
            {
                "user_id": user.id,
                "duration_seconds": duration_seconds,
                "max_devices": max_devices,
                "idempotency_key": idempotency_key,
                "created_at_utc": now,
                "paid_history_cutoff_utc": paid_history_cutoff_utc,
            },
        )

        if insert_result.rowcount == 1:
            claim = await self.get_for_user(user.id)
            if claim is None:
                raise RuntimeError("trial_claim_insert_not_visible")
            return claim, True

        claim = await self.get_for_user(user.id)
        if claim is None:
            return None, False
        if claim.status != "activation_failed":
            return claim, False

        claim_result = await self._session.execute(
            text(
                """
                UPDATE trial_claims
                SET status = 'activating', failure_code = NULL
                WHERE id = :claim_id
                  AND status = 'activation_failed'
                  AND NOT EXISTS (
                      SELECT 1
                      FROM orders
                      WHERE user_id = :user_id
                        AND is_test_order = 0
                        AND amount_minor_units > 0
                        AND (
                            paid_at IS NOT NULL
                            OR provider_payment_id IS NOT NULL
                            OR status = 'paid'
                        )
                        AND (
                            :paid_history_cutoff_utc IS NULL
                            OR COALESCE(paid_at, created_at) > :paid_history_cutoff_utc
                        )
                  )
                  AND NOT EXISTS (
                      SELECT 1
                      FROM subscriptions
                      WHERE user_id = :user_id
                        AND status = 'active'
                        AND expires_at > :created_at_utc
                        AND id != COALESCE(
                            (SELECT subscription_id FROM trial_claims WHERE id = :claim_id),
                            -1
                        )
                  )
                """
            ),
            {
                "claim_id": claim.id,
                "user_id": user.id,
                "created_at_utc": now,
                "paid_history_cutoff_utc": paid_history_cutoff_utc,
            },
        )

        if claim_result.rowcount != 1:
            refreshed = await self.get_for_user(user.id)
            return refreshed or claim, False

        claim.status = "activating"
        claim.failure_code = None
        return claim, True


class OnboardingSessionRepository:
    _OPEN_STATUSES = (
        "platform_selection",
        "app_installation",
        "access_delivery",
        "waiting_first_fetch",
        "waiting_activation",
        "failed",
    )

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def get_open_for_user(self, user_id: int) -> OnboardingSession | None:
        result = await self._session.execute(
            select(OnboardingSession)
            .where(
                OnboardingSession.user_id == user_id,
                OnboardingSession.status.in_(self._OPEN_STATUSES),
            )
            .order_by(OnboardingSession.updated_at_utc.desc(), OnboardingSession.id.desc())
            .limit(1)
        )
        return result.scalar_one_or_none()

    async def start_or_update(
        self,
        user: User,
        subscription: Subscription | None,
        platform: str | None,
        current_step: str,
        status: str,
    ) -> OnboardingSession:
        now = utc_now()
        session = await self.get_open_for_user(user.id)
        subscription_id = subscription.id if subscription is not None else None

        if session is None:
            session = OnboardingSession(
                user_id=user.id,
                origin_bot_key=get_bot_key(),
                subscription_id=subscription_id,
                platform=platform,
                current_step=current_step,
                status=status,
                issuance_request_id=uuid4().hex,
                created_at_utc=now,
                updated_at_utc=now,
            )
            self._session.add(session)
        else:
            issuance_identity_changed = session.subscription_id != subscription_id or (
                platform is not None and session.platform != platform
            )
            if issuance_identity_changed:
                self._rotate_issuance_identity(session, now)

            session.subscription_id = subscription_id
            session.platform = platform
            session.current_step = current_step
            session.status = status
            session.updated_at_utc = now
            if session.origin_bot_key is None:
                session.origin_bot_key = get_bot_key()

        if session.issuance_request_id is None:
            session.issuance_request_id = uuid4().hex

        if platform is not None:
            user.platform_preference = platform
            user.updated_at = now

        await self._session.flush()
        return session

    async def restart_open_device_issuance_for_subscription(
        self,
        user_id: int,
        subscription_id: int,
    ) -> tuple[str | None, bool]:
        result = await self._session.execute(
            select(OnboardingSession)
            .where(
                OnboardingSession.user_id == user_id,
                OnboardingSession.subscription_id == subscription_id,
                OnboardingSession.status.in_(self._OPEN_STATUSES),
            )
            .order_by(OnboardingSession.updated_at_utc.desc(), OnboardingSession.id.desc())
            .limit(1)
        )
        session = result.scalar_one_or_none()
        if session is None:
            return None, False

        if session.issuance_request_id is None:
            self._rotate_issuance_identity(session, utc_now())
            await self._session.flush()
            return session.issuance_request_id, True

        return await self.restart_device_issuance(
            session.id,
            session.issuance_request_id,
        )

    async def restart_device_issuance(
        self,
        session_id: int,
        expected_request_id: str,
    ) -> tuple[str, bool]:
        """Rotate a terminal issuance key exactly once across concurrent retries."""
        now = utc_now()
        replacement_request_id = uuid4().hex
        result = await self._session.execute(
            update(OnboardingSession)
            .where(
                OnboardingSession.id == session_id,
                OnboardingSession.status != "completed",
                OnboardingSession.issuance_request_id == expected_request_id,
            )
            .values(
                issuance_request_id=replacement_request_id,
                device_public_id=None,
                handoff_claim_id=None,
                current_step="access_delivery",
                status="access_delivery",
                updated_at_utc=now,
                last_error_code=None,
            )
        )
        if result.rowcount == 1:
            await self._session.flush()
            return replacement_request_id, True

        current = await self._session.get(OnboardingSession, session_id)
        if current is None or current.status == "completed" or current.issuance_request_id is None:
            raise RuntimeError("onboarding_session_not_restartable")

        return current.issuance_request_id, False

    @staticmethod
    def _rotate_issuance_identity(session: OnboardingSession, now: datetime) -> None:
        session.issuance_request_id = uuid4().hex
        session.device_public_id = None
        session.handoff_claim_id = None
        session.current_step = "access_delivery"
        session.status = "access_delivery"
        session.updated_at_utc = now
        session.last_error_code = None

    async def list_waiting_first_fetch(self, limit: int = 50) -> list[OnboardingSession]:
        result = await self._session.execute(
            select(OnboardingSession)
            .where(
                OnboardingSession.status.in_(["waiting_first_fetch", "waiting_activation"]),
                (
                    OnboardingSession.device_public_id.is_not(None)
                    | OnboardingSession.handoff_claim_id.is_not(None)
                ),
                OnboardingSession.subscription_id.is_not(None),
            )
            .order_by(OnboardingSession.updated_at_utc, OnboardingSession.id)
            .limit(max(limit, 1))
        )
        return list(result.scalars().all())

    async def list_recent_completed(
        self,
        cutoff: datetime,
        limit: int = 100,
    ) -> list[OnboardingSession]:
        result = await self._session.execute(
            select(OnboardingSession)
            .where(
                OnboardingSession.status == "completed",
                OnboardingSession.completed_at_utc.is_not(None),
                OnboardingSession.completed_at_utc >= to_aware_utc(cutoff),
                OnboardingSession.subscription_id.is_not(None),
            )
            .order_by(OnboardingSession.completed_at_utc, OnboardingSession.id)
            .limit(max(limit, 1))
        )
        return list(result.scalars().all())

    async def mark_completed(
        self,
        session_id: int,
        device_public_id: str,
        completed_at: datetime | None = None,
    ) -> bool:
        completed_at = to_aware_utc(completed_at or utc_now())
        result = await self._session.execute(
            update(OnboardingSession)
            .where(
                OnboardingSession.id == session_id,
                OnboardingSession.status != "completed",
            )
            .values(
                status="completed",
                current_step="completed",
                device_public_id=device_public_id,
                completed_at_utc=completed_at,
                updated_at_utc=completed_at,
                last_error_code=None,
            )
        )
        return result.rowcount == 1

    async def mark_failed(self, session_id: int, error_code: str) -> bool:
        now = utc_now()
        result = await self._session.execute(
            update(OnboardingSession)
            .where(
                OnboardingSession.id == session_id,
                OnboardingSession.status != "completed",
            )
            .values(
                status="failed",
                current_step="failed",
                updated_at_utc=now,
                last_error_code=error_code,
            )
        )
        return result.rowcount == 1

    async def abandon_stale_sessions(self, cutoff: datetime) -> int:
        now = utc_now()
        result = await self._session.execute(
            update(OnboardingSession)
            .where(
                OnboardingSession.status.in_(
                    [
                        "platform_selection",
                        "app_installation",
                        "access_delivery",
                        "waiting_first_fetch",
                        "waiting_activation",
                        "failed",
                    ]
                ),
                OnboardingSession.updated_at_utc < to_aware_utc(cutoff),
            )
            .values(
                status="abandoned",
                current_step="abandoned",
                updated_at_utc=now,
                last_error_code="stale_session",
            )
        )
        return int(result.rowcount or 0)


class DiscountRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def get_active_for_user(
        self,
        user_id: int,
        scope: str,
        now: datetime | None = None,
    ) -> UserDiscount | None:
        now = to_aware_utc(now or utc_now())
        result = await self._session.execute(
            select(UserDiscount)
            .where(
                UserDiscount.user_id == user_id,
                UserDiscount.status == "active",
                UserDiscount.scope.in_(["all", scope]),
            )
            .order_by(UserDiscount.created_at_utc.desc(), UserDiscount.id.desc())
        )

        for discount in result.scalars().all():
            if discount.starts_at_utc is not None and to_aware_utc(discount.starts_at_utc) > now:
                continue

            if discount.expires_at_utc is not None and to_aware_utc(discount.expires_at_utc) <= now:
                continue

            if discount.max_uses is not None and discount.used_count >= discount.max_uses:
                continue

            return discount

        return None


class DiscountRedemptionRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def reserve_for_order(self, order: Order) -> None:
        if order.personal_discount_id is None or order.personal_discount_amount_minor_units <= 0:
            return

        existing = await self._session.execute(
            select(DiscountRedemption).where(DiscountRedemption.order_id == order.id)
        )

        if existing.scalar_one_or_none() is not None:
            return

        now = utc_now()
        claim = await self._session.execute(
            update(UserDiscount)
            .where(
                UserDiscount.id == order.personal_discount_id,
                UserDiscount.user_id == order.user_id,
                UserDiscount.status == "active",
                (UserDiscount.starts_at_utc.is_(None) | (UserDiscount.starts_at_utc <= now)),
                (UserDiscount.expires_at_utc.is_(None) | (UserDiscount.expires_at_utc > now)),
                (
                    UserDiscount.max_uses.is_(None)
                    | (UserDiscount.used_count < UserDiscount.max_uses)
                ),
            )
            .values(used_count=UserDiscount.used_count + 1)
        )

        if claim.rowcount != 1:
            raise ValueError("Personal discount is no longer available.")

        self._session.add(
            DiscountRedemption(
                discount_id=order.personal_discount_id,
                order_id=order.id,
                status="reserved",
                reserved_at_utc=now,
                discount_amount_minor_units=order.personal_discount_amount_minor_units,
            )
        )
        await self._session.flush()

    async def apply_for_order(self, order_id: int) -> None:
        result = await self._session.execute(
            select(DiscountRedemption).where(DiscountRedemption.order_id == order_id)
        )
        redemption = result.scalar_one_or_none()

        if redemption is None or redemption.status == "applied":
            return

        if redemption.status != "reserved":
            raise RuntimeError("Discount redemption is not reserved.")

        redemption.status = "applied"
        redemption.applied_at_utc = utc_now()

    async def release_for_order(self, order_id: int) -> None:
        result = await self._session.execute(
            select(DiscountRedemption).where(DiscountRedemption.order_id == order_id)
        )
        redemption = result.scalar_one_or_none()

        if redemption is None or redemption.status == "released":
            return

        if redemption.status == "applied":
            return

        discount = await self._session.get(UserDiscount, redemption.discount_id)

        if discount is not None and discount.used_count > 0:
            discount.used_count -= 1

        redemption.status = "released"
        redemption.released_at_utc = utc_now()

    async def restore_for_paid_order(self, order_id: int) -> None:
        now = utc_now()
        restored = await self._session.execute(
            update(DiscountRedemption)
            .where(
                DiscountRedemption.order_id == order_id,
                DiscountRedemption.status == "released",
            )
            .values(
                status="reserved",
                released_at_utc=None,
                reserved_at_utc=now,
            )
            .returning(DiscountRedemption.discount_id)
        )
        discount_id = restored.scalar_one_or_none()
        if discount_id is not None:
            await self._session.execute(
                update(UserDiscount)
                .where(UserDiscount.id == discount_id)
                .values(used_count=UserDiscount.used_count + 1)
            )
            return

        result = await self._session.execute(
            select(DiscountRedemption.status).where(DiscountRedemption.order_id == order_id)
        )
        status = result.scalar_one_or_none()
        if status is None or status in {"reserved", "applied"}:
            return
        raise RuntimeError("Discount redemption has an invalid recovery state.")


class ReferralRewardRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def create_once(
        self,
        referrer_user_id: int,
        referred_user_id: int,
        source_order_id: int,
        reward_percent: int,
        reward_duration_seconds: int,
        available_at_utc: datetime,
    ) -> ReferralReward:
        existing = await self._session.execute(
            select(ReferralReward).where(ReferralReward.source_order_id == source_order_id)
        )
        reward = existing.scalar_one_or_none()

        if reward is not None:
            return reward

        reward = ReferralReward(
            referrer_user_id=referrer_user_id,
            referred_user_id=referred_user_id,
            source_order_id=source_order_id,
            reward_percent=reward_percent,
            reward_duration_seconds=reward_duration_seconds,
            status="pending_hold",
            available_at_utc=to_aware_utc(available_at_utc),
            idempotency_key=f"referral:{source_order_id}",
            created_at_utc=utc_now(),
        )
        self._session.add(reward)
        await self._session.flush()
        return reward

    async def get_for_source_order(self, source_order_id: int) -> ReferralReward | None:
        result = await self._session.execute(
            select(ReferralReward).where(ReferralReward.source_order_id == source_order_id)
        )
        return result.scalar_one_or_none()

    async def cancel_unapplied_for_source_order(self, source_order_id: int) -> str | None:
        reward = await self.get_for_source_order(source_order_id)
        if reward is None:
            return None
        if reward.status in {"pending_hold", "available", "failed"}:
            reward.status = "cancelled"
            reward.cancelled_at_utc = utc_now()
            reward.failure_code = "source_order_refunding"
        elif reward.status in {"applying", "applied"}:
            reward.status = "reversal_required"
            reward.failure_code = None
        return reward.status

    async def list_reversal_required(self, limit: int = 50) -> list[ReferralReward]:
        result = await self._session.execute(
            select(ReferralReward)
            .where(ReferralReward.status.in_(["reversal_required", "reversal_failed"]))
            .order_by(ReferralReward.applied_at_utc, ReferralReward.id)
            .limit(limit)
        )
        return list(result.scalars().all())


class SupportRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def get_by_id(self, request_id: int) -> SupportRequest | None:
        result = await self._session.execute(
            select(SupportRequest).where(SupportRequest.id == request_id)
        )
        return result.scalar_one_or_none()

    async def get_open_for_user(self, user_id: int) -> SupportRequest | None:
        result = await self._session.execute(
            select(SupportRequest)
            .where(
                SupportRequest.user_id == user_id,
                SupportRequest.status.in_(
                    ["waiting_user", "waiting_support", "waiting_user_reply"]
                ),
            )
            .order_by(SupportRequest.updated_at_utc.desc(), SupportRequest.id.desc())
            .limit(1)
        )
        return result.scalar_one_or_none()

    async def create_request(
        self,
        user: User,
        category: str,
        support_chat_id: int,
        support_root_message_id: int,
    ) -> SupportRequest:
        now = utc_now()
        request = SupportRequest(
            public_id=str(uuid4()),
            origin_bot_key=get_bot_key(),
            user_id=user.id,
            category=category,
            status="waiting_support",
            created_at_utc=now,
            updated_at_utc=now,
            support_chat_id=support_chat_id,
            support_root_message_id=support_root_message_id,
        )
        self._session.add(request)
        await self._session.flush()
        return request

    async def add_message(
        self,
        request: SupportRequest,
        direction: str,
        telegram_chat_id: int,
        telegram_message_id: int,
        message_type: str,
    ) -> SupportMessage:
        message = SupportMessage(
            support_request_id=request.id,
            bot_key=get_bot_key(),
            direction=direction,
            telegram_chat_id=telegram_chat_id,
            telegram_message_id=telegram_message_id,
            message_type=message_type,
            created_at_utc=utc_now(),
        )
        self._session.add(message)
        await self._session.flush()
        return message

    async def find_request_by_support_message(
        self,
        telegram_chat_id: int,
        telegram_message_id: int,
        bot_key: str | None = None,
    ) -> SupportRequest | None:
        bot_key = bot_key or get_bot_key()
        filters = [
            SupportMessage.telegram_chat_id == telegram_chat_id,
            SupportMessage.telegram_message_id == telegram_message_id,
        ]
        if bot_key is not None:
            filters.append(SupportMessage.bot_key == bot_key)
        result = await self._session.execute(
            select(SupportRequest)
            .join(SupportMessage, SupportMessage.support_request_id == SupportRequest.id)
            .where(*filters)
        )
        return result.scalar_one_or_none()


class NotificationDeliveryRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def claim(
        self,
        subscription_id: int,
        notification_kind: str,
        delivery_key: str,
        delivery_bot_key: str | None = None,
    ) -> NotificationDelivery | None:
        now = utc_now()
        insert_result = await self._session.execute(
            sqlite_insert(NotificationDelivery)
            .values(
                subscription_id=subscription_id,
                notification_kind=notification_kind,
                delivery_key=delivery_key,
                delivery_bot_key=delivery_bot_key or get_bot_key(),
                status="sending",
                attempt_count=1,
                claimed_at_utc=now,
                send_started_at_utc=now,
                provider_accepted_at_utc=None,
                delivered_at_utc=None,
                failed_at_utc=None,
            )
            .on_conflict_do_nothing(
                index_elements=[
                    NotificationDelivery.subscription_id,
                    NotificationDelivery.notification_kind,
                    NotificationDelivery.delivery_key,
                ]
            )
        )

        if insert_result.rowcount == 1:
            result = await self._session.execute(
                select(NotificationDelivery).where(
                    NotificationDelivery.subscription_id == subscription_id,
                    NotificationDelivery.notification_kind == notification_kind,
                    NotificationDelivery.delivery_key == delivery_key,
                )
            )
            return result.scalar_one()

        result = await self._session.execute(
            select(NotificationDelivery).where(
                NotificationDelivery.subscription_id == subscription_id,
                NotificationDelivery.notification_kind == notification_kind,
                NotificationDelivery.delivery_key == delivery_key,
            )
        )
        existing = result.scalar_one_or_none()

        if existing is None or existing.status in {"provider_accepted", "delivered"}:
            return None

        stale_before = now - timedelta(minutes=15)
        claim_result = await self._session.execute(
            update(NotificationDelivery)
            .where(
                NotificationDelivery.id == existing.id,
                (
                    (NotificationDelivery.status == "failed")
                    | (
                        (NotificationDelivery.status == "sending")
                        & (
                            NotificationDelivery.claimed_at_utc.is_(None)
                            | (NotificationDelivery.claimed_at_utc <= stale_before)
                        )
                    )
                ),
            )
            .values(
                status="sending",
                attempt_count=NotificationDelivery.attempt_count + 1,
                last_error_code=None,
                claimed_at_utc=now,
                send_started_at_utc=now,
                failed_at_utc=None,
            )
            .execution_options(synchronize_session=False)
        )

        if claim_result.rowcount != 1:
            return None

        await self._session.refresh(existing)
        return existing

    async def mark_provider_accepted(
        self, delivery_id: int, delivery_bot_key: str | None = None
    ) -> None:
        delivery = await self._session.get(NotificationDelivery, delivery_id)

        if delivery is None:
            return

        delivery.status = "provider_accepted"
        delivery.provider_accepted_at_utc = utc_now()
        if delivery_bot_key is not None:
            delivery.delivery_bot_key = delivery_bot_key
        delivery.last_error_code = None

    async def mark_delivered(self, delivery_id: int, delivery_bot_key: str | None = None) -> None:
        # Telegram confirms provider acceptance, not end-user delivery.
        await self.mark_provider_accepted(delivery_id, delivery_bot_key)

    async def mark_failed(self, delivery_id: int, error_code: str) -> None:
        delivery = await self._session.get(NotificationDelivery, delivery_id)

        if delivery is None:
            return

        delivery.status = "failed"
        delivery.failed_at_utc = utc_now()
        delivery.last_error_code = error_code[:64]


class PaymentInboxRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def receive(
        self,
        *,
        provider: str,
        provider_charge_id: str,
        invoice_payload: str,
        payer_external_id: str,
        amount_minor_units: int,
        currency: str,
        provider_occurred_at_utc: datetime | None = None,
        payment_bot_key: str | None = None,
    ) -> tuple[PaymentInbox, bool]:
        now = utc_now()
        payload_hash = hashlib.sha256(invoice_payload.encode("utf-8")).hexdigest()
        result = await self._session.execute(
            sqlite_insert(PaymentInbox)
            .values(
                provider=provider,
                provider_charge_id=provider_charge_id,
                invoice_payload_hash=payload_hash,
                invoice_payload=invoice_payload,
                payer_external_id=payer_external_id,
                amount_minor_units=amount_minor_units,
                currency=currency.upper(),
                received_at_utc=now,
                provider_occurred_at_utc=provider_occurred_at_utc,
                origin_bot_key=payment_bot_key,
                payment_bot_key=payment_bot_key,
                reconciliation_status=PAYMENT_INBOX_RECEIVED,
                attempt_count=0,
                updated_at_utc=now,
            )
            .on_conflict_do_nothing(
                index_elements=[PaymentInbox.provider, PaymentInbox.provider_charge_id]
            )
        )
        inserted = result.rowcount == 1
        query = await self._session.execute(
            select(PaymentInbox).where(
                PaymentInbox.provider == provider,
                PaymentInbox.provider_charge_id == provider_charge_id,
            )
        )
        inbox = query.scalar_one()
        if not inserted:
            same_evidence = (
                inbox.invoice_payload_hash == payload_hash
                and inbox.payer_external_id == payer_external_id
                and inbox.amount_minor_units == amount_minor_units
                and inbox.currency.upper() == currency.upper()
                and (inbox.payment_bot_key or inbox.origin_bot_key) == payment_bot_key
            )
            if not same_evidence:
                inbox.reconciliation_status = PAYMENT_INBOX_MANUAL_REVIEW
                inbox.failure_code = "provider_charge_evidence_conflict"
                inbox.processed_at_utc = now
            else:
                if inbox.invoice_payload is None:
                    inbox.invoice_payload = invoice_payload
                if inbox.provider_occurred_at_utc is None:
                    inbox.provider_occurred_at_utc = provider_occurred_at_utc
                if inbox.origin_bot_key is None:
                    inbox.origin_bot_key = payment_bot_key
                if inbox.payment_bot_key is None:
                    inbox.payment_bot_key = payment_bot_key
            inbox.updated_at_utc = now
        return inbox, inserted

    async def get_by_id(self, inbox_id: int) -> PaymentInbox | None:
        return await self._session.get(PaymentInbox, inbox_id)

    async def claim_due(
        self,
        *,
        worker_id: str,
        limit: int = 20,
        lease_seconds: int = 60,
    ) -> list[PaymentInbox]:
        now = utc_now()
        lease_until = now + timedelta(seconds=max(lease_seconds, 1))
        result = await self._session.execute(
            select(PaymentInbox.id)
            .where(
                PaymentInbox.reconciliation_status == PAYMENT_INBOX_RECEIVED,
                (
                    PaymentInbox.next_attempt_at_utc.is_(None)
                    | (PaymentInbox.next_attempt_at_utc <= now)
                ),
                (
                    PaymentInbox.claim_expires_at_utc.is_(None)
                    | (PaymentInbox.claim_expires_at_utc <= now)
                ),
            )
            .order_by(PaymentInbox.received_at_utc, PaymentInbox.id)
            .limit(limit)
        )
        claimed_ids: list[int] = []
        for inbox_id in result.scalars().all():
            claim = await self._session.execute(
                update(PaymentInbox)
                .where(
                    PaymentInbox.id == inbox_id,
                    PaymentInbox.reconciliation_status == PAYMENT_INBOX_RECEIVED,
                    (
                        PaymentInbox.claim_expires_at_utc.is_(None)
                        | (PaymentInbox.claim_expires_at_utc <= now)
                    ),
                )
                .values(
                    claimed_by=worker_id,
                    claim_expires_at_utc=lease_until,
                    attempt_count=PaymentInbox.attempt_count + 1,
                    last_attempt_at_utc=now,
                    updated_at_utc=now,
                )
            )
            if claim.rowcount == 1:
                claimed_ids.append(int(inbox_id))

        if not claimed_ids:
            return []
        claimed = await self._session.execute(
            select(PaymentInbox)
            .where(PaymentInbox.id.in_(claimed_ids))
            .order_by(PaymentInbox.received_at_utc, PaymentInbox.id)
        )
        return list(claimed.scalars().all())

    async def mark_matched(self, inbox: PaymentInbox, order_id: int) -> None:
        inbox.matched_order_id = order_id
        inbox.reconciliation_status = PAYMENT_INBOX_MATCHED
        inbox.failure_code = None
        inbox.claimed_by = None
        inbox.claim_expires_at_utc = None
        inbox.next_attempt_at_utc = None
        inbox.updated_at_utc = utc_now()

    async def mark_applied(self, inbox: PaymentInbox, order_id: int) -> None:
        inbox.matched_order_id = order_id
        inbox.reconciliation_status = PAYMENT_INBOX_APPLIED
        inbox.failure_code = None
        inbox.claimed_by = None
        inbox.claim_expires_at_utc = None
        inbox.next_attempt_at_utc = None
        inbox.processed_at_utc = utc_now()
        inbox.updated_at_utc = inbox.processed_at_utc

    async def mark_applied_for_order(self, order_id: int) -> None:
        now = utc_now()
        await self._session.execute(
            update(PaymentInbox)
            .where(
                PaymentInbox.matched_order_id == order_id,
                PaymentInbox.reconciliation_status == PAYMENT_INBOX_MATCHED,
            )
            .values(
                reconciliation_status=PAYMENT_INBOX_APPLIED,
                failure_code=None,
                claimed_by=None,
                claim_expires_at_utc=None,
                next_attempt_at_utc=None,
                processed_at_utc=now,
                updated_at_utc=now,
            )
        )

    async def mark_retry(
        self,
        inbox: PaymentInbox,
        failure_code: str,
        *,
        delay_seconds: int,
    ) -> None:
        now = utc_now()
        inbox.failure_code = failure_code[:64]
        inbox.claimed_by = None
        inbox.claim_expires_at_utc = None
        inbox.next_attempt_at_utc = now + timedelta(seconds=max(delay_seconds, 1))
        inbox.updated_at_utc = now

    async def mark_manual_review(self, inbox: PaymentInbox, failure_code: str) -> None:
        inbox.reconciliation_status = PAYMENT_INBOX_MANUAL_REVIEW
        inbox.failure_code = failure_code[:64]
        inbox.claimed_by = None
        inbox.claim_expires_at_utc = None
        inbox.next_attempt_at_utc = None
        inbox.processed_at_utc = utc_now()
        inbox.updated_at_utc = inbox.processed_at_utc

    async def list_unresolved(self, limit: int = 100) -> list[PaymentInbox]:
        result = await self._session.execute(
            select(PaymentInbox)
            .where(
                PaymentInbox.reconciliation_status.in_(
                    ["received", "matched", "manual_review", "refund_required"]
                )
            )
            .order_by(PaymentInbox.received_at_utc, PaymentInbox.id)
            .limit(limit)
        )
        return list(result.scalars().all())


class EntitlementOperationRepository:
    _active_states = (
        "pending",
        "claimed",
        "external_unknown",
        "external_applied",
        "local_commit_pending",
        "failed_retriable",
        "compensating",
    )

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def get_by_public_id(self, public_id: str) -> EntitlementOperation | None:
        result = await self._session.execute(
            select(EntitlementOperation).where(EntitlementOperation.public_id == public_id)
        )
        return result.scalar_one_or_none()

    async def get_for_source(
        self,
        *,
        source_entity_type: str,
        source_entity_id: str,
        operation_type: str,
    ) -> EntitlementOperation | None:
        result = await self._session.execute(
            select(EntitlementOperation).where(
                EntitlementOperation.source_entity_type == source_entity_type,
                EntitlementOperation.source_entity_id == source_entity_id,
                EntitlementOperation.operation_type == operation_type,
            )
        )
        return result.scalar_one_or_none()

    async def create_once(
        self,
        *,
        user_id: int,
        operation_type: str,
        source_entity_type: str,
        source_entity_id: str,
        idempotency_key: str,
        subscription_id: int | None,
        duration_delta_seconds: int,
        requested_device_limit: int | None,
        requested_status: str | None,
        observed_valid_until_utc: datetime | None = None,
    ) -> EntitlementOperation:
        existing = await self.get_for_source(
            source_entity_type=source_entity_type,
            source_entity_id=source_entity_id,
            operation_type=operation_type,
        )
        if existing is not None:
            return existing
        now = utc_now()
        operation = EntitlementOperation(
            public_id=str(uuid4()),
            subscription_id=subscription_id,
            user_id=user_id,
            operation_type=operation_type,
            source_entity_type=source_entity_type,
            source_entity_id=source_entity_id,
            idempotency_key=idempotency_key,
            duration_delta_seconds=duration_delta_seconds,
            requested_device_limit=requested_device_limit,
            requested_status=requested_status,
            observed_valid_until_utc=(
                to_aware_utc(observed_valid_until_utc)
                if observed_valid_until_utc is not None
                else None
            ),
            state="pending",
            created_at_utc=now,
            updated_at_utc=now,
        )
        self._session.add(operation)
        await self._session.flush()
        return operation

    async def claim(
        self,
        operation: EntitlementOperation,
        *,
        owner: str,
        lease_seconds: int = 120,
    ) -> bool:
        now = utc_now()
        expires = now + timedelta(seconds=max(lease_seconds, 1))
        result = await self._session.execute(
            update(EntitlementOperation)
            .where(
                EntitlementOperation.id == operation.id,
                EntitlementOperation.state.in_(
                    ["pending", "failed_retriable", "external_unknown", "external_applied"]
                ),
                (
                    EntitlementOperation.claim_expires_at_utc.is_(None)
                    | (EntitlementOperation.claim_expires_at_utc <= now)
                    | (EntitlementOperation.claimed_by == owner)
                ),
            )
            .values(
                state="claimed",
                claimed_by=owner,
                claim_expires_at_utc=expires,
                attempt_count=EntitlementOperation.attempt_count + 1,
                last_error_code=None,
                updated_at_utc=now,
            )
            .execution_options(synchronize_session=False)
        )
        if result.rowcount != 1:
            return False
        await self._session.refresh(operation)
        return True

    async def set_intent(
        self,
        operation: EntitlementOperation,
        *,
        subscription_id: int,
        expected_version: int,
        intended_valid_until_utc: datetime,
        requested_device_limit: int,
        requested_status: str,
    ) -> None:
        operation.subscription_id = subscription_id
        operation.expected_version = expected_version
        operation.intended_valid_until_utc = to_aware_utc(intended_valid_until_utc)
        operation.requested_device_limit = requested_device_limit
        operation.requested_status = requested_status
        operation.updated_at_utc = utc_now()

    async def mark_request_sent(self, operation: EntitlementOperation) -> None:
        now = utc_now()
        operation.state = "external_unknown"
        operation.external_request_sent_at_utc = now
        operation.updated_at_utc = now

    async def mark_external_applied(
        self,
        operation: EntitlementOperation,
        *,
        result_version: int,
        result_status: str,
        result_valid_until_utc: datetime,
        result_device_limit: int,
        applied_at_utc: datetime | None = None,
        external_subscription_public_guid: str | None = None,
    ) -> None:
        now = utc_now()
        operation.state = "external_applied"
        operation.external_result_version = result_version
        operation.external_result_status = result_status
        operation.external_result_valid_until_utc = to_aware_utc(result_valid_until_utc)
        operation.external_result_device_limit = result_device_limit
        if external_subscription_public_guid is not None:
            operation.external_subscription_public_guid = external_subscription_public_guid
        operation.external_applied_at_utc = to_aware_utc(applied_at_utc or now)
        operation.updated_at_utc = now

    async def mark_completed(self, operation: EntitlementOperation) -> None:
        now = utc_now()
        operation.state = "completed"
        operation.local_commit_completed_at_utc = now
        operation.claimed_by = None
        operation.claim_expires_at_utc = None
        operation.last_error_code = None
        operation.updated_at_utc = now

    async def mark_failed_retriable(self, operation: EntitlementOperation, error_code: str) -> None:
        now = utc_now()
        operation.state = "failed_retriable"
        operation.last_error_code = error_code[:64]
        operation.last_error_at_utc = now
        operation.claimed_by = None
        operation.claim_expires_at_utc = None
        operation.updated_at_utc = now

    async def mark_manual_review(self, operation: EntitlementOperation, error_code: str) -> None:
        now = utc_now()
        operation.state = "manual_review"
        operation.last_error_code = error_code[:64]
        operation.last_error_at_utc = now
        operation.claimed_by = None
        operation.claim_expires_at_utc = None
        operation.updated_at_utc = now

    async def rearm_after_reconciliation(self, operation: EntitlementOperation) -> bool:
        if (
            operation.state != "manual_review"
            or operation.last_error_code != "reconciliation_blocked"
            or operation.external_request_sent_at_utc is not None
            or operation.external_result_version is not None
            or operation.external_result_status is not None
            or operation.external_result_valid_until_utc is not None
            or operation.external_result_device_limit is not None
        ):
            return False
        if operation.subscription_id is not None and await self.has_active_for_subscription(
            operation.subscription_id,
            exclude_operation_id=operation.id,
        ):
            return False

        now = utc_now()
        operation.state = "pending"
        operation.claimed_by = None
        operation.claim_expires_at_utc = None
        operation.last_error_code = None
        operation.last_error_at_utc = None
        operation.updated_at_utc = now
        await self._session.flush()
        return True

    async def mark_superseded(self, operation: EntitlementOperation, reason: str) -> None:
        now = utc_now()
        operation.state = "superseded"
        operation.last_error_code = reason[:64]
        operation.claimed_by = None
        operation.claim_expires_at_utc = None
        operation.updated_at_utc = now

    async def find_matching_unfinished_result(
        self,
        *,
        subscription_id: int,
        version: int,
        status: str,
        valid_until_utc: datetime,
        max_device_tokens: int,
    ) -> EntitlementOperation | None:
        result = await self._session.execute(
            select(EntitlementOperation)
            .where(
                EntitlementOperation.subscription_id == subscription_id,
                EntitlementOperation.state.in_(
                    ["external_applied", "local_commit_pending", "external_unknown"]
                ),
                EntitlementOperation.external_result_version == version,
                EntitlementOperation.external_result_status == status,
                EntitlementOperation.external_result_valid_until_utc
                == to_aware_utc(valid_until_utc),
                EntitlementOperation.external_result_device_limit == max_device_tokens,
            )
            .order_by(EntitlementOperation.updated_at_utc.desc())
            .limit(1)
        )
        return result.scalar_one_or_none()

    async def list_for_subscription(
        self, subscription_id: int, limit: int = 100
    ) -> list[EntitlementOperation]:
        result = await self._session.execute(
            select(EntitlementOperation)
            .where(EntitlementOperation.subscription_id == subscription_id)
            .order_by(EntitlementOperation.created_at_utc.desc())
            .limit(limit)
        )
        return list(result.scalars().all())

    async def list_recoverable(self, limit: int = 100) -> list[EntitlementOperation]:
        now = utc_now()
        result = await self._session.execute(
            select(EntitlementOperation)
            .where(
                EntitlementOperation.state.in_(self._active_states),
                (
                    EntitlementOperation.claim_expires_at_utc.is_(None)
                    | (EntitlementOperation.claim_expires_at_utc <= now)
                ),
            )
            .order_by(EntitlementOperation.updated_at_utc, EntitlementOperation.id)
            .limit(limit)
        )
        return list(result.scalars().all())

    async def has_active_for_subscription(
        self, subscription_id: int, exclude_operation_id: int | None = None
    ) -> bool:
        filters = [
            EntitlementOperation.subscription_id == subscription_id,
            EntitlementOperation.state.in_(self._active_states),
        ]
        if exclude_operation_id is not None:
            filters.append(EntitlementOperation.id != exclude_operation_id)
        result = await self._session.execute(
            select(func.count(EntitlementOperation.id)).where(*filters)
        )
        return int(result.scalar_one()) > 0


class RefundOperationRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def get_for_order(self, order_id: int) -> RefundOperation | None:
        result = await self._session.execute(
            select(RefundOperation).where(RefundOperation.order_id == order_id)
        )
        return result.scalar_one_or_none()

    async def create_once(
        self,
        *,
        order: Order,
        subscription_id: int | None,
        provider_charge_id: str,
    ) -> RefundOperation:
        existing = await self.get_for_order(order.id)
        if existing is not None:
            return existing
        now = utc_now()
        operation = RefundOperation(
            public_id=str(uuid4()),
            order_id=order.id,
            subscription_id=subscription_id,
            user_id=order.user_id,
            idempotency_key=f"refund:{order.public_order_id}",
            state=REFUND_OPERATION_PREPARED,
            provider=order.provider,
            provider_charge_reference_hash=hashlib.sha256(
                provider_charge_id.encode("utf-8")
            ).hexdigest(),
            created_at_utc=now,
            updated_at_utc=now,
        )
        self._session.add(operation)
        await self._session.flush()
        return operation

    async def mark_provider_requested(self, operation: RefundOperation) -> None:
        now = utc_now()
        operation.state = "provider_unknown"
        operation.provider_requested_at_utc = now
        operation.attempt_count += 1
        operation.updated_at_utc = now

    async def mark_provider_refunded(self, operation: RefundOperation) -> None:
        now = utc_now()
        operation.state = "provider_refunded"
        operation.provider_refunded_at_utc = now
        operation.last_error_code = None
        operation.updated_at_utc = now

    async def mark_compensation_pending(
        self, operation: RefundOperation, entitlement_operation_id: int
    ) -> None:
        operation.state = "compensation_pending"
        operation.compensation_operation_id = entitlement_operation_id
        operation.updated_at_utc = utc_now()

    async def mark_completed(self, operation: RefundOperation) -> None:
        now = utc_now()
        operation.state = "completed"
        operation.completed_at_utc = now
        operation.last_error_code = None
        operation.updated_at_utc = now

    async def mark_manual_review(self, operation: RefundOperation, error_code: str) -> None:
        operation.state = "manual_review"
        operation.last_error_code = error_code[:64]
        operation.updated_at_utc = utc_now()

    async def list_recoverable(
        self,
        *,
        provider_unknown_cutoff: datetime,
        limit: int = 100,
    ) -> list[RefundOperation]:
        result = await self._session.execute(
            select(RefundOperation)
            .where(
                (RefundOperation.state.in_(["provider_refunded", "compensation_pending"]))
                | (
                    (RefundOperation.state == "provider_unknown")
                    & (RefundOperation.updated_at_utc <= to_aware_utc(provider_unknown_cutoff))
                )
            )
            .order_by(RefundOperation.updated_at_utc, RefundOperation.id)
            .limit(limit)
        )
        return list(result.scalars().all())

    async def has_blocking_for_user(
        self,
        user_id: int,
        *,
        exclude_operation_id: int | None = None,
    ) -> bool:
        filters = [
            RefundOperation.user_id == user_id,
            RefundOperation.state != "completed",
        ]
        if exclude_operation_id is not None:
            filters.append(RefundOperation.id != exclude_operation_id)
        result = await self._session.execute(select(func.count(RefundOperation.id)).where(*filters))
        return int(result.scalar_one()) > 0


class RefundPlanRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def get_for_order(self, order_id: int) -> RefundPlan | None:
        result = await self._session.execute(
            select(RefundPlan).where(RefundPlan.order_id == order_id)
        )
        return result.scalar_one_or_none()

    async def get_by_confirmation_hash(self, token_hash: str) -> RefundPlan | None:
        result = await self._session.execute(
            select(RefundPlan).where(RefundPlan.confirmation_token_hash == token_hash)
        )
        return result.scalar_one_or_none()

    async def create_or_refresh(
        self,
        *,
        operation: RefundOperation,
        order: Order,
        subscription_id: int | None,
        expected_current_entitlement_version: int | None,
        previous_status: str | None,
        previous_valid_until_utc: datetime | None,
        previous_max_devices: int | None,
        target_status: str,
        target_valid_until_utc: datetime | None,
        target_max_devices: int | None,
        evidence_hash: str,
        confirmation_token_hash: str,
        confirmation_expires_at_utc: datetime,
        created_by_admin_telegram_id: int | None,
    ) -> RefundPlan:
        plan = await self.get_for_order(order.id)
        now = utc_now()
        values = {
            "subscription_id": subscription_id,
            "expected_current_entitlement_version": expected_current_entitlement_version,
            "previous_status": previous_status,
            "previous_valid_until_utc": previous_valid_until_utc,
            "previous_max_devices": previous_max_devices,
            "target_status": target_status,
            "target_valid_until_utc": target_valid_until_utc,
            "target_max_devices": target_max_devices,
            "source_order_kind": order.order_kind,
            "computation_version": 1,
            "evidence_hash": evidence_hash,
            "state": "prepared",
            "confirmation_token_hash": confirmation_token_hash,
            "confirmation_expires_at_utc": confirmation_expires_at_utc,
            "created_by_admin_telegram_id": created_by_admin_telegram_id,
            "updated_at_utc": now,
            "failure_code": None,
        }
        if plan is None:
            plan = RefundPlan(
                public_id=str(uuid4()),
                refund_operation_id=operation.id,
                order_id=order.id,
                created_at_utc=now,
                **values,
            )
            self._session.add(plan)
            await self._session.flush()
            return plan
        if plan.state not in {"prepared", "manual_review"}:
            return plan
        for key, value in values.items():
            setattr(plan, key, value)
        await self._session.flush()
        return plan

    async def mark_confirmed(self, plan: RefundPlan) -> None:
        now = utc_now()
        plan.state = "confirmed"
        plan.confirmed_at_utc = now
        plan.confirmation_token_hash = None
        plan.updated_at_utc = now

    async def mark_state(self, plan: RefundPlan, state: str, error_code: str | None = None) -> None:
        plan.state = state
        plan.failure_code = error_code[:64] if error_code else None
        plan.updated_at_utc = utc_now()


class NotificationOutboxRepository:
    _allowed_payload_keys = frozenset(
        {
            "entitlement_status",
            "entitlement_version",
            "lifecycle_reason_code",
            "local_max_device_tokens",
            "local_status",
            "local_valid_until_utc",
            "local_version",
            "mode",
            "notification_key",
            "operation_public_id",
            "order_public_id",
            "public_guid",
            "reason_code",
            "remote_max_device_tokens",
            "remote_status",
            "remote_valid_until_utc",
            "remote_version",
            "subscription_status",
            "suggested_action",
            "telegram_id",
            "username",
            "campaign_id",
            "message_text",
        }
    )

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def enqueue_once(
        self,
        *,
        idempotency_key: str,
        notification_kind: str,
        user_id: int | None = None,
        subscription_id: int | None = None,
        order_id: int | None = None,
        broadcast_campaign_id: int | None = None,
        bot_key: str | None = None,
        payload: dict[str, object] | None = None,
    ) -> NotificationOutbox:
        item, _ = await self.enqueue_once_with_status(
            idempotency_key=idempotency_key,
            notification_kind=notification_kind,
            user_id=user_id,
            subscription_id=subscription_id,
            order_id=order_id,
            broadcast_campaign_id=broadcast_campaign_id,
            bot_key=bot_key,
            payload=payload,
        )
        return item

    async def enqueue_once_with_status(
        self,
        *,
        idempotency_key: str,
        notification_kind: str,
        user_id: int | None = None,
        subscription_id: int | None = None,
        order_id: int | None = None,
        broadcast_campaign_id: int | None = None,
        bot_key: str | None = None,
        payload: dict[str, object] | None = None,
    ) -> tuple[NotificationOutbox, bool]:
        existing_result = await self._session.execute(
            select(NotificationOutbox).where(NotificationOutbox.idempotency_key == idempotency_key)
        )
        existing = existing_result.scalar_one_or_none()
        if existing is not None:
            return existing, False
        safe_payload = None
        if payload:
            safe_payload = json.dumps(
                {key: value for key, value in payload.items() if key in self._allowed_payload_keys},
                ensure_ascii=False,
                sort_keys=True,
            )
        now = utc_now()
        item = NotificationOutbox(
            public_id=str(uuid4()),
            idempotency_key=idempotency_key,
            user_id=user_id,
            subscription_id=subscription_id,
            order_id=order_id,
            broadcast_campaign_id=broadcast_campaign_id,
            bot_key=bot_key or get_bot_key(),
            notification_kind=notification_kind,
            payload_json=safe_payload,
            state="pending",
            available_at_utc=now,
            created_at_utc=now,
            updated_at_utc=now,
        )
        self._session.add(item)
        await self._session.flush()
        return item, True

    async def bulk_enqueue_broadcast(
        self,
        *,
        campaign_id: int,
        campaign_public_id: str,
        bot_key: str,
        user_ids: list[int],
    ) -> int:
        if not user_ids:
            return 0
        now = utc_now()
        payload_json = json.dumps(
            {"campaign_id": campaign_public_id},
            ensure_ascii=False,
            sort_keys=True,
        )
        result = await self._session.execute(
            sqlite_insert(NotificationOutbox)
            .values(
                [
                    {
                        "public_id": str(uuid4()),
                        "idempotency_key": f"admin-broadcast:{campaign_public_id}:{user_id}",
                        "user_id": user_id,
                        "broadcast_campaign_id": campaign_id,
                        "bot_key": bot_key,
                        "notification_kind": "admin_broadcast",
                        "payload_json": payload_json,
                        "state": "pending",
                        "attempt_count": 0,
                        "available_at_utc": now,
                        "created_at_utc": now,
                        "updated_at_utc": now,
                    }
                    for user_id in user_ids
                ]
            )
            .on_conflict_do_nothing(index_elements=[NotificationOutbox.idempotency_key])
            .returning(NotificationOutbox.id)
        )
        return len(list(result.scalars()))

    async def claim_batch(
        self, owner_timeout_minutes: int = 15, limit: int = 50
    ) -> list[NotificationOutbox]:
        now = utc_now()
        stale = now - timedelta(minutes=max(owner_timeout_minutes, 1))
        result = await self._session.execute(
            select(NotificationOutbox)
            .where(
                NotificationOutbox.available_at_utc <= now,
                (
                    (NotificationOutbox.state.in_(["pending", "failed"]))
                    | (
                        (NotificationOutbox.state == "sending")
                        & (NotificationOutbox.claimed_at_utc <= stale)
                    )
                ),
            )
            .order_by(NotificationOutbox.available_at_utc, NotificationOutbox.id)
            .limit(limit)
        )
        items = list(result.scalars().all())
        for item in items:
            item.state = "sending"
            item.claimed_at_utc = now
            item.attempt_count += 1
            item.updated_at_utc = now
        return items

    async def mark_provider_accepted(
        self, item: NotificationOutbox, delivery_bot_key: str | None = None
    ) -> None:
        now = utc_now()
        item.state = "provider_accepted"
        item.provider_accepted_at_utc = now
        if delivery_bot_key is not None:
            item.delivery_bot_key = delivery_bot_key
        item.last_error_code = None
        item.updated_at_utc = now

    async def mark_failed(
        self,
        item: NotificationOutbox,
        error_code: str,
        *,
        retry_after_seconds: int | None = None,
    ) -> None:
        now = utc_now()
        item.state = "failed"
        item.failed_at_utc = now
        item.last_error_code = error_code[:64]
        if retry_after_seconds is not None:
            item.available_at_utc = now + timedelta(seconds=max(retry_after_seconds, 1))
        item.updated_at_utc = now

    async def mark_terminal_failed(self, item: NotificationOutbox, error_code: str) -> None:
        now = utc_now()
        item.state = "terminal_failed"
        item.failed_at_utc = now
        item.last_error_code = error_code[:64]
        item.updated_at_utc = now

    async def mark_failed_bounded(
        self,
        item: NotificationOutbox,
        error_code: str,
        *,
        max_attempts: int,
        retry_base_seconds: int,
        retry_max_seconds: int,
        retry_after_seconds: int | None = None,
    ) -> None:
        if item.attempt_count >= max(max_attempts, 1):
            await self.mark_terminal_failed(item, error_code)
            return

        exponential_delay = min(
            max(retry_base_seconds, 1) * (2 ** max(item.attempt_count - 1, 0)),
            max(retry_max_seconds, 1),
        )
        requested_delay = max(retry_after_seconds or 0, exponential_delay)
        await self.mark_failed(
            item,
            error_code,
            retry_after_seconds=requested_delay,
        )


class AuditRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def add(
        self,
        event_type: str,
        telegram_id: int | None = None,
        order_id: int | None = None,
        subscription_id: int | None = None,
        public_guid: str | None = None,
        error_code: str | None = None,
        details_json: str | None = None,
        correlation_id: str | None = None,
    ) -> AuditEvent:
        event = AuditEvent(
            created_at_utc=utc_now(),
            event_type=event_type,
            telegram_id=telegram_id,
            order_id=order_id,
            subscription_id=subscription_id,
            public_guid=public_guid,
            error_code=error_code,
            details_json=details_json,
            correlation_id=correlation_id or get_correlation_id(),
            bot_key=get_bot_key(),
        )
        self._session.add(event)
        await self._session.flush()
        return event


class AccessOperationLeaseRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def acquire(
        self,
        *,
        user_id: int,
        owner_kind: str,
        owner_key: str,
        lease_seconds: int = 120,
    ) -> bool:
        now = utc_now()
        expires_at = now + timedelta(seconds=max(lease_seconds, 1))
        result = await self._session.execute(
            sqlite_insert(AccessOperationLease)
            .values(
                user_id=user_id,
                owner_kind=owner_kind,
                owner_key=owner_key,
                lease_expires_at_utc=expires_at,
                updated_at_utc=now,
            )
            .on_conflict_do_update(
                index_elements=[AccessOperationLease.user_id],
                set_={
                    "owner_kind": owner_kind,
                    "owner_key": owner_key,
                    "lease_expires_at_utc": expires_at,
                    "updated_at_utc": now,
                },
                where=AccessOperationLease.lease_expires_at_utc <= now,
            )
        )
        return result.rowcount == 1

    async def renew(
        self,
        *,
        user_id: int,
        owner_key: str,
        lease_seconds: int = 120,
    ) -> bool:
        now = utc_now()
        expires_at = now + timedelta(seconds=max(lease_seconds, 1))
        result = await self._session.execute(
            update(AccessOperationLease)
            .where(
                AccessOperationLease.user_id == user_id,
                AccessOperationLease.owner_key == owner_key,
            )
            .values(
                lease_expires_at_utc=expires_at,
                updated_at_utc=now,
            )
        )
        return result.rowcount == 1

    async def release(self, *, user_id: int, owner_key: str) -> None:
        await self._session.execute(
            delete(AccessOperationLease).where(
                AccessOperationLease.user_id == user_id,
                AccessOperationLease.owner_key == owner_key,
            )
        )


class ProductEventRepository:
    _allowed_payload_keys = frozenset(
        {
            "order_kind",
            "platform",
            "reason_code",
            "period_count",
            "max_devices",
            "source",
        }
    )

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def exists_idempotency_key(self, idempotency_key: str) -> bool:
        result = await self._session.execute(
            select(func.count(ProductEvent.id)).where(
                ProductEvent.idempotency_key == idempotency_key
            )
        )
        return int(result.scalar_one()) > 0

    async def record(
        self,
        *,
        event_name: str,
        user_id: int | None = None,
        idempotency_key: str | None = None,
        correlation_id: str | None = None,
        payload: dict[str, object] | None = None,
    ) -> ProductEvent:
        correlation_id = correlation_id or get_correlation_id()
        safe_payload = None
        if payload:
            filtered = {
                key: value for key, value in payload.items() if key in self._allowed_payload_keys
            }
            safe_payload = json.dumps(filtered, ensure_ascii=False, sort_keys=True)

        if idempotency_key is not None:
            await self._session.execute(
                sqlite_insert(ProductEvent)
                .values(
                    public_id=str(uuid4()),
                    user_id=user_id,
                    event_name=event_name,
                    occurred_at_utc=utc_now(),
                    correlation_id=correlation_id,
                    idempotency_key=idempotency_key,
                    payload_json=safe_payload,
                    bot_key=get_bot_key(),
                )
                .on_conflict_do_nothing(index_elements=[ProductEvent.idempotency_key])
            )
            result = await self._session.execute(
                select(ProductEvent).where(ProductEvent.idempotency_key == idempotency_key)
            )
            return result.scalar_one()

        event = ProductEvent(
            public_id=str(uuid4()),
            user_id=user_id,
            event_name=event_name,
            occurred_at_utc=utc_now(),
            correlation_id=correlation_id,
            idempotency_key=None,
            payload_json=safe_payload,
            bot_key=get_bot_key(),
        )
        self._session.add(event)
        await self._session.flush()
        return event
