from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import secrets
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import ClassVar, Protocol
from uuid import uuid4

from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from vpn_access_bot.advertising_readiness import CommercePolicyRepository
from vpn_access_bot.commerce import CalculatedPrice, PricingService
from vpn_access_bot.config import Settings
from vpn_access_bot.constants import (
    ENTITLEMENT_STATUS_ACTIVE,
    ENTITLEMENT_STATUS_DISABLED,
    ENTITLEMENT_STATUS_EXPIRED,
    ORDER_ACTIVATION_ALLOWED_STATUSES,
    ORDER_ACTIVATION_RETRY_STATUSES,
    ORDER_KIND_EXTEND,
    ORDER_KIND_EXTEND_AND_UPGRADE,
    ORDER_KIND_PURCHASE,
    ORDER_KIND_RESUME,
    ORDER_KIND_UPGRADE_DEVICES,
    ORDER_KINDS_WITH_PAID_DURATION,
    ORDER_REFUNDABLE_STATUSES,
    ORDER_STATUS_ACTIVATING,
    ORDER_STATUS_ACTIVATION_FAILED,
    ORDER_STATUS_EXPIRED,
    ORDER_STATUS_PAID,
    ORDER_STATUS_PAYMENT_RECEIVED,
    ORDER_STATUS_PENDING,
    ORDER_STATUS_REFUNDED,
    ORDER_STATUS_REFUNDING,
    PAYMENT_MODE_MANUAL,
    PAYMENT_MODE_TELEGRAM_STARS,
    PAYMENT_MODE_YOOKASSA_SBP,
    REFERRAL_REWARD_PERCENT,
    SUBSCRIPTION_STATUS_ACTIVE,
    SUBSCRIPTION_STATUS_DISABLED,
    SUBSCRIPTION_STATUS_EXPIRED,
    TELEGRAM_STARS_CURRENCY,
    TELEGRAM_STARS_PROVIDER_TOKEN,
    TELEGRAM_STARS_PROVIDERS,
    TRIAL_DURATION_SECONDS,
    TRIAL_MAX_DEVICES,
    TRIAL_STATUS_ACTIVATING,
    TRIAL_STATUS_ACTIVATION_FAILED,
    TRIAL_STATUS_ACTIVE,
    TRIAL_STATUS_CONVERTED,
    TRIAL_STATUS_EXPIRED,
    TRIAL_STATUS_REVOKED,
)
from vpn_access_bot.entitlement_lifecycle import project_reconciled_lifecycle
from vpn_access_bot.expiration import calculate_expiration_snapshot
from vpn_access_bot.mediator_client import (
    DeviceTokenListItem,
    MediatorClient,
    MediatorClientError,
)
from vpn_access_bot.models import (
    AuditEvent,
    EntitlementOperation,
    Order,
    PurchaseQuote,
    Subscription,
    Tariff,
    TrialClaim,
    utc_now,
)
from vpn_access_bot.operations import AppliedEntitlement, EntitlementOperationCoordinator
from vpn_access_bot.product_catalog import ProductCatalog
from vpn_access_bot.repositories import (
    AccessOperationLeaseRepository,
    CommercialEntitlementAdjustmentRepository,
    CommercialEntitlementSegmentRepository,
    DeviceResetRepository,
    DiscountRedemptionRepository,
    DiscountRepository,
    EntitlementOperationRepository,
    EntitlementRepository,
    NotificationOutboxRepository,
    OnboardingSessionRepository,
    OrderApplicationRepository,
    OrderRepository,
    PaymentInboxRepository,
    ProductEventRepository,
    PurchaseQuoteRepository,
    ReferralRewardRepository,
    RefundOperationRepository,
    RefundPlanRepository,
    SubscriptionRepository,
    TariffRepository,
    TrialClaimRepository,
    UserRepository,
    to_aware_utc,
)
from vpn_access_bot.trial import TrialEligibilityReason, TrialEligibilityService

logger = logging.getLogger(__name__)


class SessionFactory(Protocol):
    def __call__(self) -> object:
        raise NotImplementedError


@dataclass(frozen=True)
class InvoicePrice:
    label: str
    amount: int


@dataclass(frozen=True)
class TelegramStarsInvoice:
    title: str
    description: str
    payload: str
    provider_token: str
    currency: str
    prices: list[InvoicePrice]


@dataclass(frozen=True)
class PaymentRecordOutcome:
    order_id: int | None
    needs_activation: bool
    already_paid: bool = False
    subscription: Subscription | None = None
    inbox_status: str = "matched"
    failure_code: str | None = None


@dataclass(frozen=True)
class ActivationOutcome:
    order_id: int
    subscription: Subscription | None
    activated: bool
    already_paid: bool = False
    failure_code: str | None = None

    @property
    def failure_message(self) -> str | None:
        return self.failure_code


@dataclass(frozen=True)
class AdminEntitlementAdjustmentOutcome:
    operation_public_id: str
    subscription: Subscription
    version: int
    status: str


@dataclass(frozen=True)
class ReconciliationRepairOutcome:
    operation_public_id: str
    subscription: Subscription
    version: int
    status: str
    mode: str


@dataclass(frozen=True)
class RefundCandidate:
    order: Order
    is_eligible: bool
    charge_id: str | None = None
    already_refunded: bool = False
    reason: str | None = None
    refund_operation_public_id: str | None = None
    confirmation_token: str | None = None
    target_status: str | None = None
    target_valid_until_utc: datetime | None = None
    target_max_devices: int | None = None
    referral_action: str | None = None


class PurchaseService:
    def __init__(
        self,
        session: AsyncSession,
        settings: Settings,
        mediator_client: MediatorClient,
    ) -> None:
        self._session = session
        self._settings = settings
        self._mediator_client = mediator_client

    @staticmethod
    def _require_reconciliation_healthy(subscription: Subscription) -> None:
        if subscription.reconciliation_state != "healthy":
            raise ValueError("subscription_reconciliation_blocked")

    async def _get_order_target_subscription(
        self,
        order: Order,
    ) -> Subscription | None:
        if order.target_subscription_id is None:
            if order.order_kind != ORDER_KIND_PURCHASE:
                raise RuntimeError("Non-purchase order has no target subscription.")
            return None

        subscription = await SubscriptionRepository(self._session).get_by_id(
            order.target_subscription_id
        )
        if subscription is None or subscription.user_id != order.user_id:
            raise RuntimeError("Order target subscription is invalid.")
        return subscription

    async def create_quote(
        self,
        telegram_id: int,
        username: str | None,
        first_name: str | None,
        period_count: int,
        max_devices: int,
        order_kind: str,
        target_subscription_id: int | None = None,
    ) -> PurchaseQuote:
        user = await UserRepository(self._session).get_or_create_from_message_user(
            telegram_id=telegram_id,
            username=username,
            first_name=first_name,
        )
        subscription_repository = SubscriptionRepository(self._session)
        primary_subscription = await subscription_repository.get_primary_for_user(user)
        target_subscription = (
            await subscription_repository.get_by_id(target_subscription_id)
            if target_subscription_id is not None
            else primary_subscription
        )
        if target_subscription is not None and target_subscription.user_id != user.id:
            raise ValueError("invalid_target_subscription")
        if target_subscription is not None:
            self._require_reconciliation_healthy(target_subscription)

        now = utc_now()
        supported_operations = {
            ORDER_KIND_PURCHASE,
            ORDER_KIND_EXTEND,
            ORDER_KIND_EXTEND_AND_UPGRADE,
            ORDER_KIND_RESUME,
            ORDER_KIND_UPGRADE_DEVICES,
        }
        if order_kind not in supported_operations:
            raise ValueError("unsupported_order_kind")

        if order_kind == ORDER_KIND_PURCHASE:
            if target_subscription is not None:
                raise ValueError("subscription_state_changed")
            current_max_devices = 0
            current_valid_until = None
        else:
            if target_subscription is None:
                raise ValueError("subscription_required")
            if target_subscription.status == SUBSCRIPTION_STATUS_DISABLED:
                raise ValueError("disabled_subscription_requires_support")
            current_max_devices = target_subscription.max_devices
            current_valid_until = to_aware_utc(target_subscription.expires_at)
            is_currently_active = (
                target_subscription.status == SUBSCRIPTION_STATUS_ACTIVE
                and current_valid_until > now
            )
            is_expired = (
                target_subscription.status == SUBSCRIPTION_STATUS_EXPIRED
                or current_valid_until <= now
            )
            if (
                order_kind
                in {
                    ORDER_KIND_EXTEND,
                    ORDER_KIND_EXTEND_AND_UPGRADE,
                    ORDER_KIND_UPGRADE_DEVICES,
                }
                and not is_currently_active
            ):
                raise ValueError("subscription_state_changed")
            if order_kind == ORDER_KIND_RESUME and not is_expired:
                raise ValueError("subscription_state_changed")
            if order_kind in {ORDER_KIND_EXTEND, ORDER_KIND_RESUME}:
                if max_devices != current_max_devices:
                    raise ValueError("device_limit_change_requires_upgrade")
            if order_kind == ORDER_KIND_UPGRADE_DEVICES and period_count != 0:
                raise ValueError("upgrade_period_must_be_zero")
            if order_kind == ORDER_KIND_EXTEND_AND_UPGRADE and max_devices <= current_max_devices:
                raise ValueError("device_limit_must_increase")

        remaining_paid_seconds_value = 0
        if target_subscription is not None and order_kind in {
            ORDER_KIND_UPGRADE_DEVICES,
            ORDER_KIND_EXTEND_AND_UPGRADE,
        }:
            remaining_paid_seconds_value = await CommercialEntitlementSegmentRepository(
                self._session
            ).remaining_device_upgrade_seconds(target_subscription)
            if order_kind == ORDER_KIND_UPGRADE_DEVICES and remaining_paid_seconds_value <= 0:
                raise ValueError("paid_access_required_for_device_upgrade")

        current_entitlement = (
            await EntitlementRepository(self._session).get_for_subscription(target_subscription.id)
            if target_subscription is not None
            else None
        )
        active_discount = await DiscountRepository(self._session).get_active_for_user(
            user.id,
            order_kind,
        )
        personal_discount_bps = active_discount.discount_bps if active_discount else 0
        price = PricingService(self._settings).calculate_operation(
            operation_kind=order_kind,
            period_count=period_count,
            requested_max_devices=max_devices,
            current_max_devices=current_max_devices,
            remaining_paid_seconds=remaining_paid_seconds_value,
            personal_discount_bps=personal_discount_bps,
        )
        expires_at = now + timedelta(minutes=self._settings.quote_ttl_minutes)
        active_trial = await TrialClaimRepository(self._session).get_for_user(user.id)
        trial_seconds_remaining = 0
        if (
            active_trial is not None
            and active_trial.status == TRIAL_STATUS_ACTIVE
            and active_trial.ends_at_utc is not None
        ):
            trial_seconds_remaining = max(
                int((to_aware_utc(active_trial.ends_at_utc) - now).total_seconds()),
                0,
            )

        quote = await PurchaseQuoteRepository(self._session).create(
            user=user,
            period_count=price.period_count,
            duration_days=price.duration_days,
            max_devices=price.max_devices,
            amount_minor_units=price.amount_minor_units,
            currency=price.currency,
            pricing_version=price.pricing_version,
            target_subscription_id=target_subscription.id if target_subscription else None,
            order_kind=order_kind,
            expires_at=expires_at,
            base_entitlement_version=current_entitlement.version if current_entitlement else None,
            base_valid_until_utc=current_valid_until,
            base_max_devices=current_max_devices if target_subscription else None,
            upgrade_amount_minor_units=price.upgrade_amount_minor_units,
            extension_amount_minor_units=price.extension_amount_minor_units,
            personal_discount_id=active_discount.id if active_discount else None,
            personal_discount_bps=price.personal_discount_bps,
            personal_discount_amount_minor_units=price.personal_discount_amount_minor_units,
            referral_eligible=(
                order_kind in ORDER_KINDS_WITH_PAID_DURATION and price.amount_minor_units > 0
            ),
            trial_claim_id=active_trial.id if active_trial else None,
            trial_seconds_remaining_at_quote=trial_seconds_remaining,
            remaining_paid_seconds_at_quote=remaining_paid_seconds_value,
        )
        event_repository = ProductEventRepository(self._session)
        await event_repository.record(
            event_name="purchase_started",
            user_id=user.id,
            idempotency_key=f"purchase_started:{quote.public_quote_id}",
            payload={"order_kind": order_kind},
        )
        if order_kind in {ORDER_KIND_EXTEND, ORDER_KIND_RESUME}:
            await event_repository.record(
                event_name="renewal_started",
                user_id=user.id,
                idempotency_key=f"renewal_started:{quote.public_quote_id}",
                payload={"order_kind": order_kind},
            )
        await event_repository.record(
            event_name="quote_created",
            user_id=user.id,
            idempotency_key=f"quote_created:{quote.public_quote_id}",
            payload={
                "order_kind": order_kind,
                "period_count": price.period_count,
                "max_devices": price.max_devices,
            },
        )
        if price.period_count > 0:
            await event_repository.record(
                event_name="period_selected",
                user_id=user.id,
                idempotency_key=f"period_selected:{quote.public_quote_id}",
                payload={"period_count": price.period_count, "order_kind": order_kind},
            )
        if order_kind == ORDER_KIND_UPGRADE_DEVICES:
            await event_repository.record(
                event_name="upgrade_started",
                user_id=user.id,
                idempotency_key=f"upgrade_started:{quote.public_quote_id}",
                payload={"max_devices": price.max_devices},
            )
        return quote

    async def create_order_from_quote(
        self,
        public_quote_id: str,
        actor_telegram_id: int,
        payment_bot_key: str | None = None,
        payment_provider: str | None = None,
    ) -> Order:
        provider = payment_provider or self._settings.payment_mode
        if provider not in {
            PAYMENT_MODE_MANUAL,
            PAYMENT_MODE_TELEGRAM_STARS,
            PAYMENT_MODE_YOOKASSA_SBP,
        }:
            raise ValueError("unsupported_payment_provider")
        if provider == PAYMENT_MODE_YOOKASSA_SBP and not self._settings.external_payment_enabled:
            raise ValueError("external_payment_disabled")
        user = await UserRepository(self._session).get_by_telegram_id(actor_telegram_id)
        quote = (
            await PurchaseQuoteRepository(self._session).get_by_public_id_for_user(
                public_quote_id,
                user.id,
            )
            if user is not None
            else None
        )

        if quote is None:
            logger.warning(
                "Quote ownership check rejected: actor_telegram_id=%s",
                actor_telegram_id,
            )
            raise ValueError("Quote was not found.")

        offer = self.calculate_quote_offer(quote, provider)
        pricing_provider = (
            PAYMENT_MODE_TELEGRAM_STARS if provider == PAYMENT_MODE_MANUAL else provider
        )
        current_pricing_identity = ProductCatalog.from_settings(
            self._settings
        ).pricing_identity_for_provider(pricing_provider)
        expected_quote_identity = ProductCatalog.from_settings(self._settings).pricing_identity
        if not quote.is_test_order and quote.pricing_version != expected_quote_identity:
            raise ValueError("pricing_configuration_changed")

        now = utc_now()
        target: Subscription | None = None
        if quote.target_subscription_id is not None:
            target = await SubscriptionRepository(self._session).get_by_id(
                quote.target_subscription_id
            )
            if target is None or target.user_id != quote.user_id:
                raise ValueError("Quote target subscription is invalid.")
            self._require_reconciliation_healthy(target)

        if quote.consumed_at_utc is not None:
            repository = OrderRepository(self._session)
            existing = await repository.get_for_quote(quote.id)

            if existing is not None:
                if existing.provider != provider:
                    raise ValueError("payment_provider_already_selected")
                if (
                    provider == PAYMENT_MODE_TELEGRAM_STARS
                    and existing.amount_minor_units > 0
                    and payment_bot_key is not None
                    and not await repository.claim_payment_bot(existing, payment_bot_key)
                ):
                    raise ValueError("payment_bot_conflict")
                return existing

            raise ValueError("Quote was already consumed.")

        if to_aware_utc(quote.expires_at_utc) <= now:
            raise ValueError("Quote expired.")

        if target is not None:
            current_entitlement = await EntitlementRepository(self._session).get_for_subscription(
                target.id
            )

            if quote.base_entitlement_version is not None and (
                current_entitlement is None
                or current_entitlement.version != quote.base_entitlement_version
            ):
                raise ValueError("Quote became stale.")

            if quote.base_max_devices is not None and target.max_devices != quote.base_max_devices:
                raise ValueError("Quote became stale.")

            if quote.base_valid_until_utc is not None and to_aware_utc(
                target.expires_at
            ) != to_aware_utc(quote.base_valid_until_utc):
                raise ValueError("Quote became stale.")

        purchased_duration_days = (
            0
            if quote.order_kind == ORDER_KIND_UPGRADE_DEVICES
            else (quote.requested_duration_days or quote.duration_days)
        )
        current_expires_at = target.expires_at if quote.target_subscription_id is not None else None
        expiration = calculate_expiration_snapshot(
            current_expires_at_utc=current_expires_at,
            captured_now_utc=now,
            purchased_duration_days=purchased_duration_days,
            order_kind=quote.order_kind,
            business_timezone=self._settings.subscription_time_zone,
            configured_policy_version=self._settings.expiration_policy_version,
            policy_effective_at_utc=self._settings.expiration_policy_effective_at_utc,
        )
        quote_claimed = await PurchaseQuoteRepository(self._session).try_consume(quote.id, now)
        if not quote_claimed:
            existing = await OrderRepository(self._session).get_for_quote(quote.id)
            if existing is not None and existing.user_id == quote.user_id:
                if existing.provider != provider:
                    raise ValueError("payment_provider_already_selected")
                return existing
            raise ValueError("Quote was already consumed.")
        quote.consumed_at_utc = now
        try:
            order = await OrderRepository(self._session).create_order_from_quote(
                quote=quote,
                provider=provider,
                invoice_payload=f"order:{uuid4().hex}",
                expires_at=now + timedelta(minutes=self._settings.order_ttl_minutes),
                amount_minor_units=offer.amount_minor_units,
                currency=offer.currency,
                pricing_version=current_pricing_identity,
                upgrade_amount_minor_units=offer.upgrade_amount_minor_units,
                extension_amount_minor_units=offer.extension_amount_minor_units,
                price_before_personal_discount=offer.price_before_personal_discount,
                personal_discount_amount_minor_units=offer.personal_discount_amount_minor_units,
                base_expires_at_utc=expiration.base_expires_at_utc,
                purchased_duration_days=expiration.purchased_duration_days,
                expiration_policy_version=expiration.expiration_policy_version,
                target_expires_at_utc=expiration.target_expires_at_utc,
            )
            if (
                provider == PAYMENT_MODE_TELEGRAM_STARS
                and order.amount_minor_units > 0
                and payment_bot_key is not None
            ):
                claimed = await OrderRepository(self._session).claim_payment_bot(
                    order, payment_bot_key
                )
                if not claimed:
                    raise ValueError("payment_bot_conflict")
        except IntegrityError as exception:
            await self._session.rollback()
            repository = OrderRepository(self._session)
            existing = await repository.get_for_quote(quote.id)
            if existing is None:
                existing = await repository.get_relevant_unfinished_for_user(quote.user_id)
            if existing is not None:
                if existing.provider != provider:
                    raise ValueError("payment_provider_already_selected") from exception
                if (
                    provider == PAYMENT_MODE_TELEGRAM_STARS
                    and existing.amount_minor_units > 0
                    and payment_bot_key is not None
                    and not await repository.claim_payment_bot(existing, payment_bot_key)
                ):
                    raise ValueError("payment_bot_conflict") from exception
                return existing
            raise RuntimeError("concurrent_order_creation_conflict") from exception
        await DiscountRedemptionRepository(self._session).reserve_for_order(order)
        if order.amount_minor_units == 0:
            order_created_event = "complimentary_order_created"
        elif provider == PAYMENT_MODE_TELEGRAM_STARS:
            order_created_event = "invoice_created"
        elif provider == PAYMENT_MODE_YOOKASSA_SBP:
            order_created_event = "external_checkout_created"
        else:
            order_created_event = "manual_order_created"

        await ProductEventRepository(self._session).record(
            event_name=order_created_event,
            user_id=order.user_id,
            idempotency_key=f"order_created:{order.public_order_id}",
            payload={"order_kind": order.order_kind},
        )
        if order.amount_minor_units > 0:
            await ProductEventRepository(self._session).record(
                event_name="payment_started",
                user_id=order.user_id,
                idempotency_key=f"payment_started:{order.public_order_id}",
                payload={"order_kind": order.order_kind},
            )
        return order

    def calculate_quote_offer(self, quote: PurchaseQuote, provider: str):
        if provider in {PAYMENT_MODE_MANUAL, PAYMENT_MODE_TELEGRAM_STARS}:
            return CalculatedPrice(
                period_count=quote.period_count,
                duration_days=quote.duration_days,
                max_devices=quote.max_devices,
                amount_minor_units=quote.amount_minor_units,
                currency=quote.currency,
                pricing_version=quote.pricing_version,
                upgrade_amount_minor_units=quote.upgrade_amount_minor_units,
                extension_amount_minor_units=quote.extension_amount_minor_units,
                price_before_personal_discount=quote.price_before_personal_discount,
                personal_discount_bps=quote.personal_discount_bps,
                personal_discount_amount_minor_units=quote.personal_discount_amount_minor_units,
            )
        pricing_provider = (
            PAYMENT_MODE_TELEGRAM_STARS if provider == PAYMENT_MODE_MANUAL else provider
        )
        return PricingService(self._settings).calculate_quote_offer(quote, pricing_provider)

    async def create_order_for_tariff(
        self,
        telegram_id: int,
        username: str | None,
        first_name: str | None,
        tariff_code: str,
    ) -> Order:
        user = await UserRepository(self._session).get_or_create_from_message_user(
            telegram_id=telegram_id,
            username=username,
            first_name=first_name,
        )
        tariff = await TariffRepository(self._session).get_active_by_code(tariff_code)

        if tariff is None:
            raise ValueError("Tariff was not found.")

        amount, currency = self._order_amount_and_currency(tariff)

        order = await OrderRepository(self._session).create_pending_order(
            user=user,
            tariff=tariff,
            provider=self._settings.payment_mode,
            amount_minor_units=amount,
            currency=currency,
        )
        expiration = calculate_expiration_snapshot(
            current_expires_at_utc=None,
            captured_now_utc=order.created_at,
            purchased_duration_days=order.duration_days,
            order_kind=order.order_kind,
            business_timezone=self._settings.subscription_time_zone,
            configured_policy_version=self._settings.expiration_policy_version,
            policy_effective_at_utc=self._settings.expiration_policy_effective_at_utc,
        )
        order.base_expires_at_utc = expiration.base_expires_at_utc
        order.purchased_duration_days = expiration.purchased_duration_days
        order.expiration_policy_version = expiration.expiration_policy_version
        order.target_expires_at_utc = expiration.target_expires_at_utc
        return order

    def build_telegram_stars_invoice(self, order: Order) -> TelegramStarsInvoice:
        if order.provider not in TELEGRAM_STARS_PROVIDERS:
            raise ValueError("Order is not configured for Telegram Stars.")

        if order.currency.upper() != TELEGRAM_STARS_CURRENCY:
            raise ValueError("Telegram Stars invoices must use XTR currency.")

        if order.amount_minor_units <= 0:
            raise ValueError("Complimentary orders must not create Telegram Stars invoices.")

        title = self._settings.product_name
        description = (
            f"{order.tariff.title}. {order.tariff.description}"
            if order.tariff is not None
            else (
                f"Доступ к {self._settings.product_name} на {order.duration_days} дней, "
                f"до {order.selected_max_devices} устройств."
            )
        )

        return TelegramStarsInvoice(
            title=title,
            description=description,
            payload=order.invoice_payload,
            provider_token=TELEGRAM_STARS_PROVIDER_TOKEN,
            currency=TELEGRAM_STARS_CURRENCY,
            prices=[
                InvoicePrice(
                    label=title,
                    amount=order.amount_minor_units,
                ),
            ],
        )

    async def validate_order_for_invoice(
        self,
        payload: str,
        amount_minor_units: int,
        currency: str,
        payer_telegram_id: int,
    ) -> tuple[Order | None, str | None]:
        user = await UserRepository(self._session).get_by_telegram_id(payer_telegram_id)
        order_repository = OrderRepository(self._session)
        order = (
            await order_repository.get_for_payment_payload_for_user(
                payload,
                user.id,
            )
            if user is not None
            else None
        )

        if order is None:
            logger.warning(
                "Payment ownership check rejected: payer_telegram_id=%s",
                payer_telegram_id,
            )
            return None, "Заказ не найден. Создайте новый заказ."

        if order.status != ORDER_STATUS_PENDING:
            return None, "Этот заказ уже нельзя оплатить. Создайте новый заказ."

        current_pricing_identity = ProductCatalog.from_settings(self._settings).pricing_identity
        if not order.is_test_order and order.pricing_version != current_pricing_identity:
            return None, "Условия заказа изменились. Создайте новый заказ."

        if order.expires_at_utc is not None and to_aware_utc(order.expires_at_utc) <= utc_now():
            await order_repository.mark_expired(order)
            await DiscountRedemptionRepository(self._session).release_for_order(order.id)
            return None, "Срок заказа закончился. Создайте новый заказ."

        if (
            order.amount_minor_units != amount_minor_units
            or order.currency.upper() != currency.upper()
        ):
            return None, "Сумма заказа изменилась. Создайте новый заказ."

        if order.target_subscription_id is not None:
            target = await SubscriptionRepository(self._session).get_by_id(
                order.target_subscription_id
            )
            if target is None or target.user_id != order.user_id:
                return None, "Подписка заказа больше недоступна. Создайте новый заказ."
            if target.reconciliation_state != "healthy":
                return (
                    None,
                    "Синхронизация доступа ещё не завершена. Оплата временно недоступна; "
                    "повторно оплачивать не нужно.",
                )

        return order, None

    async def validate_order_before_checkout(
        self,
        payload: str,
        amount_minor_units: int,
        currency: str,
        payer_telegram_id: int,
        payment_bot_key: str | None = None,
    ) -> tuple[bool, str | None]:
        order, error_message = await self.validate_order_for_invoice(
            payload,
            amount_minor_units,
            currency,
            payer_telegram_id,
        )
        if order is None:
            return False, error_message

        order_repository = OrderRepository(self._session)
        if payment_bot_key is not None:
            payment_channel_matches = await order_repository.claim_payment_bot(
                order, payment_bot_key
            )
            if not payment_channel_matches:
                return False, "Этот счёт был выставлен другим ботом. Откройте исходный чат."

        authorized_at = utc_now()
        authorized = await order_repository.try_mark_checkout_authorized(
            order,
            authorized_at_utc=authorized_at,
            authorized_until_utc=authorized_at
            + timedelta(seconds=self._settings.checkout_authorization_grace_seconds),
        )
        if not authorized:
            return False, "Срок заказа закончился. Создайте новый заказ."
        return True, None

    async def record_successful_telegram_stars_payment(
        self,
        payload: str,
        amount_minor_units: int,
        currency: str,
        telegram_payment_charge_id: str,
        payer_telegram_id: int,
        *,
        provider_occurred_at_utc: datetime | None = None,
        payment_bot_key: str | None = None,
    ) -> PaymentRecordOutcome:
        inbox_repository = PaymentInboxRepository(self._session)
        inbox, _ = await inbox_repository.receive(
            provider=PAYMENT_MODE_TELEGRAM_STARS,
            provider_charge_id=telegram_payment_charge_id,
            invoice_payload=payload,
            payer_external_id=str(payer_telegram_id),
            amount_minor_units=amount_minor_units,
            currency=currency,
            provider_occurred_at_utc=provider_occurred_at_utc,
            payment_bot_key=payment_bot_key,
        )
        # Provider evidence is a separate durable boundary. A process crash after this
        # commit is recovered by the payment reconciliation worker.
        await self._session.commit()
        outcome = await self.reconcile_payment_inbox_by_id(inbox.id)
        if outcome.failure_code == "order_not_found_or_owner_mismatch":
            raise ValueError("Order was not found.")
        if outcome.failure_code == "payment_evidence_mismatch":
            raise ValueError("Payment details do not match the order.")
        if outcome.failure_code == "provider_charge_evidence_conflict":
            raise ValueError("Payment evidence conflicts with an existing provider charge.")
        return outcome

    async def reconcile_payment_inbox_by_id(self, inbox_id: int) -> PaymentRecordOutcome:
        inbox_repository = PaymentInboxRepository(self._session)
        inbox = await inbox_repository.get_by_id(inbox_id)
        if inbox is None:
            raise ValueError("Payment inbox item was not found.")

        if inbox.reconciliation_status == "manual_review":
            return PaymentRecordOutcome(
                order_id=inbox.matched_order_id,
                needs_activation=False,
                inbox_status="manual_review",
                failure_code=inbox.failure_code or "manual_review_required",
            )

        try:
            payer_telegram_id = int(inbox.payer_external_id)
        except ValueError:
            await inbox_repository.mark_manual_review(inbox, "invalid_payer_external_id")
            await self._session.commit()
            return PaymentRecordOutcome(
                order_id=None,
                needs_activation=False,
                inbox_status="manual_review",
                failure_code="invalid_payer_external_id",
            )

        order_repository = OrderRepository(self._session)
        user = await UserRepository(self._session).get_by_telegram_id(payer_telegram_id)
        order = None
        if user is not None and inbox.invoice_payload is not None:
            order = await order_repository.get_for_payment_payload_for_user(
                inbox.invoice_payload, user.id
            )
        elif user is not None:
            # Version-14 inbox rows did not persist the raw payload. Recover only from an
            # exact, unique hash match owned by the same payer; never guess across users.
            order = await order_repository.get_unique_for_payment_payload_hash_for_user(
                inbox.invoice_payload_hash, user.id
            )
            if order is not None:
                inbox.invoice_payload = order.invoice_payload

        if inbox.invoice_payload is None and order is None:
            await inbox_repository.mark_manual_review(inbox, "invoice_payload_unavailable")
            await self._session.commit()
            return PaymentRecordOutcome(
                order_id=inbox.matched_order_id,
                needs_activation=False,
                inbox_status="manual_review",
                failure_code="invoice_payload_unavailable",
            )

        if order is None:
            await inbox_repository.mark_manual_review(inbox, "order_not_found_or_owner_mismatch")
            await self._session.commit()
            logger.warning(
                "Successful payment could not be matched: payer_telegram_id=%s",
                payer_telegram_id,
            )
            return PaymentRecordOutcome(
                order_id=None,
                needs_activation=False,
                inbox_status="manual_review",
                failure_code="order_not_found_or_owner_mismatch",
            )

        if (
            order.provider != inbox.provider
            or order.amount_minor_units != inbox.amount_minor_units
            or order.currency.upper() != inbox.currency.upper()
        ):
            await inbox_repository.mark_manual_review(inbox, "payment_evidence_mismatch")
            await self._session.commit()
            return PaymentRecordOutcome(
                order_id=order.id,
                needs_activation=False,
                inbox_status="manual_review",
                failure_code="payment_evidence_mismatch",
            )

        evidence_bot_key = getattr(inbox, "payment_bot_key", None) or inbox.origin_bot_key
        if evidence_bot_key is not None:
            payment_channel_matches = await order_repository.claim_payment_bot(
                order, evidence_bot_key
            )
            if not payment_channel_matches:
                await inbox_repository.mark_manual_review(inbox, "payment_bot_mismatch")
                await self._session.commit()
                return PaymentRecordOutcome(
                    order_id=order.id,
                    needs_activation=False,
                    inbox_status="manual_review",
                    failure_code="payment_bot_mismatch",
                )

        existing_payment = await order_repository.get_by_provider_payment_id(
            order.provider, inbox.provider_charge_id
        )
        if existing_payment is not None and existing_payment.id != order.id:
            await inbox_repository.mark_manual_review(inbox, "charge_bound_to_other_order")
            await self._session.commit()
            return PaymentRecordOutcome(
                order_id=order.id,
                needs_activation=False,
                inbox_status="manual_review",
                failure_code="charge_bound_to_other_order",
            )

        if order.status == ORDER_STATUS_PAID:
            await inbox_repository.mark_applied(inbox, order.id)
            return PaymentRecordOutcome(
                order_id=order.id,
                needs_activation=False,
                already_paid=True,
                subscription=await self._subscription_for_applied_order(order),
                inbox_status="applied",
            )

        if order.status in {ORDER_STATUS_REFUNDING, ORDER_STATUS_REFUNDED}:
            await inbox_repository.mark_manual_review(inbox, "payment_for_refunding_order")
            await self._session.commit()
            return PaymentRecordOutcome(
                order_id=order.id,
                needs_activation=False,
                inbox_status="manual_review",
                failure_code="payment_for_refunding_order",
            )

        if order.status == ORDER_STATUS_EXPIRED:
            occurred_at = inbox.provider_occurred_at_utc or inbox.received_at_utc
            authorized_until = order.checkout_authorized_until_utc
            authorized_at = order.checkout_authorized_at_utc
            authorized_payment = (
                authorized_at is not None
                and authorized_until is not None
                and to_aware_utc(authorized_at)
                <= to_aware_utc(occurred_at)
                <= to_aware_utc(authorized_until)
            )
            if not authorized_payment:
                await inbox_repository.mark_manual_review(inbox, "late_payment_for_expired_order")
                await self._session.commit()
                return PaymentRecordOutcome(
                    order_id=order.id,
                    needs_activation=False,
                    inbox_status="manual_review",
                    failure_code="late_payment_for_expired_order",
                )
            await DiscountRedemptionRepository(self._session).restore_for_paid_order(order.id)
            order.status = ORDER_STATUS_PENDING
            order.cancelled_at_utc = None

        if order.status == ORDER_STATUS_PENDING:
            await order_repository.mark_payment_received(
                order,
                inbox.provider_charge_id,
                paid_at=inbox.provider_occurred_at_utc or inbox.received_at_utc,
            )
        elif order.status in ORDER_ACTIVATION_ALLOWED_STATUSES:
            if order.provider_payment_id is None:
                order.provider_payment_id = inbox.provider_charge_id
            if order.paid_at is None:
                order.paid_at = inbox.provider_occurred_at_utc or inbox.received_at_utc
        else:
            await inbox_repository.mark_manual_review(inbox, "invalid_order_state")
            await self._session.commit()
            return PaymentRecordOutcome(
                order_id=order.id,
                needs_activation=False,
                inbox_status="manual_review",
                failure_code="invalid_order_state",
            )

        await ProductEventRepository(self._session).record(
            event_name="payment_succeeded",
            user_id=order.user_id,
            idempotency_key=f"payment_succeeded:{order.provider}:{inbox.provider_charge_id}",
            payload={"order_kind": order.order_kind},
        )
        await ProductEventRepository(self._session).record(
            event_name="payment_completed",
            user_id=order.user_id,
            idempotency_key=f"payment_completed:{order.provider}:{inbox.provider_charge_id}",
            payload={"order_kind": order.order_kind},
        )
        await inbox_repository.mark_matched(inbox, order.id)
        return PaymentRecordOutcome(
            order_id=order.id,
            needs_activation=True,
            inbox_status="matched",
        )

    async def prepare_manual_order_for_activation(
        self,
        order_id: int,
        admin_telegram_id: int,
    ) -> PaymentRecordOutcome:
        order_repository = OrderRepository(self._session)
        order = await order_repository.get_by_id(order_id)

        if order is None:
            raise ValueError("Order was not found.")

        if order.status == ORDER_STATUS_PAID:
            return PaymentRecordOutcome(
                order_id=order.id,
                needs_activation=False,
                already_paid=True,
                subscription=await self._subscription_for_applied_order(order),
            )

        if order.status == ORDER_STATUS_REFUNDED:
            raise ValueError("Order was already refunded.")

        if order.status == ORDER_STATUS_PENDING:
            if order.provider != PAYMENT_MODE_MANUAL:
                raise ValueError("Only manual pending orders can be approved manually.")

            await order_repository.mark_payment_received(
                order,
                provider_payment_id=f"manual:{admin_telegram_id}:{order.id}",
            )
            return PaymentRecordOutcome(order_id=order.id, needs_activation=True)

        if order.status in ORDER_ACTIVATION_ALLOWED_STATUSES:
            return PaymentRecordOutcome(order_id=order.id, needs_activation=True)

        raise ValueError("Order cannot be approved from its current status.")

    async def prepare_complimentary_order_for_activation(
        self,
        order_id: int,
        actor_telegram_id: int,
    ) -> PaymentRecordOutcome:
        order_repository = OrderRepository(self._session)
        user = await UserRepository(self._session).get_by_telegram_id(actor_telegram_id)
        order = (
            await order_repository.get_by_id_for_user(order_id, user.id)
            if user is not None
            else None
        )

        if order is None:
            logger.warning(
                "Complimentary order ownership check rejected: actor_telegram_id=%s",
                actor_telegram_id,
            )
            raise ValueError("Order was not found.")

        if (
            order.amount_minor_units != 0
            or order.personal_discount_bps != 10_000
            or order.personal_discount_id is None
        ):
            raise ValueError("Order is not a complimentary personal-discount order.")

        if order.status == ORDER_STATUS_PAID:
            return PaymentRecordOutcome(
                order_id=order.id,
                needs_activation=False,
                already_paid=True,
                subscription=await self._subscription_for_applied_order(order),
            )

        if order.status == ORDER_STATUS_EXPIRED:
            raise ValueError("Order expired. Please create a new order.")

        if (
            order.status == ORDER_STATUS_PENDING
            and order.expires_at_utc is not None
            and to_aware_utc(order.expires_at_utc) <= utc_now()
        ):
            await order_repository.mark_expired(order)
            await DiscountRedemptionRepository(self._session).release_for_order(order.id)
            raise ValueError("Order expired. Please create a new order.")

        if order.status == ORDER_STATUS_PENDING:
            await order_repository.mark_complimentary_ready(order)
            await ProductEventRepository(self._session).record(
                event_name="complimentary_order_claimed",
                user_id=order.user_id,
                idempotency_key=f"complimentary_order_claimed:{order.public_order_id}",
                payload={"order_kind": order.order_kind},
            )
            return PaymentRecordOutcome(order_id=order.id, needs_activation=True)

        if order.status in ORDER_ACTIVATION_ALLOWED_STATUSES:
            return PaymentRecordOutcome(order_id=order.id, needs_activation=True)

        raise ValueError("Complimentary order cannot be activated from its current status.")

    async def activate_order(self, order: Order) -> ActivationOutcome:
        order_repository = OrderRepository(self._session)
        if order.status == ORDER_STATUS_PAID:
            return ActivationOutcome(
                order_id=order.id,
                subscription=await self._subscription_for_applied_order(order),
                activated=False,
                already_paid=True,
            )
        if order.status in {ORDER_STATUS_REFUNDING, ORDER_STATUS_REFUNDED}:
            raise ValueError("order_refund_in_progress")
        if order.status not in ORDER_ACTIVATION_ALLOWED_STATUSES:
            raise ValueError("order_has_not_received_payment")

        target_subscription = await self._get_order_target_subscription(order)
        if (
            target_subscription is not None
            and target_subscription.reconciliation_state != "healthy"
        ):
            await order_repository.mark_activation_failed(order, "reconciliation_blocked")
            await NotificationOutboxRepository(self._session).enqueue_once(
                idempotency_key=f"paid-order-reconciliation-blocked:{order.public_order_id}",
                notification_kind="operator_paid_order_reconciliation_blocked",
                user_id=order.user_id,
                subscription_id=target_subscription.id,
                order_id=order.id,
                payload={
                    "order_public_id": order.public_order_id,
                    "public_guid": target_subscription.public_guid,
                    "reason_code": target_subscription.reconciliation_reason
                    or "reconciliation_blocked",
                },
            )
            await self._session.commit()
            return ActivationOutcome(
                order_id=order.id,
                subscription=None,
                activated=False,
                failure_code="reconciliation_blocked",
            )

        owner_key = f"order:{order.public_order_id}"
        lease_repository = AccessOperationLeaseRepository(self._session)
        lease_acquired = await lease_repository.acquire(
            user_id=order.user_id,
            owner_kind="order",
            owner_key=owner_key,
        )
        if not lease_acquired:
            return ActivationOutcome(
                order_id=order.id,
                subscription=None,
                activated=False,
                failure_code="access_operation_in_progress",
            )

        operation = await EntitlementOperationCoordinator(
            self._session, self._mediator_client
        ).prepare_order(order)
        if order.status != ORDER_STATUS_ACTIVATING:
            claimed = await order_repository.transition_status(
                order.id,
                [ORDER_STATUS_PAYMENT_RECEIVED, ORDER_STATUS_ACTIVATION_FAILED],
                ORDER_STATUS_ACTIVATING,
            )
            if not claimed:
                await lease_repository.release(user_id=order.user_id, owner_key=owner_key)
                refreshed = await order_repository.get_by_id(order.id)
                if refreshed is not None and refreshed.status == ORDER_STATUS_PAID:
                    return await self.activate_order(refreshed)
                return ActivationOutcome(
                    order_id=order.id,
                    subscription=None,
                    activated=False,
                    failure_code="order_already_processing",
                )
        await self._session.commit()
        order = await order_repository.get_by_id(order.id)
        operation = await EntitlementOperationRepository(self._session).get_by_public_id(
            operation.public_id
        )
        if order is None or operation is None:
            raise RuntimeError("activation_state_disappeared_after_prepare")

        order_id = order.id
        order_user_id = order.user_id
        try:
            subscription, operation = await self._activate_subscription_for_order(order, operation)
        except MediatorClientError as exception:
            error_code = exception.error_code or "mediator_unavailable"
            await self._session.rollback()
            failed_order = await OrderRepository(self._session).get_by_id(order_id)
            failed_operation = await EntitlementOperationRepository(self._session).get_for_source(
                source_entity_type="order",
                source_entity_id=order.public_order_id,
                operation_type=(
                    "paid_activation" if order.amount_minor_units > 0 else "complimentary"
                ),
            )
            if failed_order is not None and (
                failed_operation is None
                or failed_operation.state not in {"external_unknown", "external_applied"}
            ):
                await OrderRepository(self._session).mark_activation_failed(
                    failed_order, error_code
                )
            await AccessOperationLeaseRepository(self._session).release(
                user_id=order_user_id,
                owner_key=owner_key,
            )
            await self._session.commit()
            return ActivationOutcome(
                order_id=order_id,
                subscription=None,
                activated=False,
                failure_code=error_code,
            )
        except Exception:
            logger.exception("Order activation finalization failed: order_id=%s", order_id)
            # The durable operation result is intentionally retained. Recovery can finish the
            # local transaction without applying the purchased delta a second time.
            await self._session.rollback()
            failed_operation = await EntitlementOperationRepository(self._session).get_by_public_id(
                operation.public_id
            )
            if failed_operation is not None and failed_operation.state == "external_applied":
                failed_operation.state = "local_commit_pending"
                failed_operation.last_error_code = "activation_finalize_failed"
                failed_operation.updated_at_utc = utc_now()
            await AccessOperationLeaseRepository(self._session).release(
                user_id=order_user_id,
                owner_key=owner_key,
            )
            await self._session.commit()
            return ActivationOutcome(
                order_id=order_id,
                subscription=None,
                activated=False,
                failure_code="activation_finalize_failed",
            )

        await order_repository.mark_paid(order)
        await PaymentInboxRepository(self._session).mark_applied_for_order(order.id)
        await DiscountRedemptionRepository(self._session).apply_for_order(order.id)
        await ProductEventRepository(self._session).record(
            event_name="entitlement_activated",
            user_id=order.user_id,
            idempotency_key=f"entitlement_activated:{order.id}",
            payload={"order_kind": order.order_kind},
        )
        await ProductEventRepository(self._session).record(
            event_name="activation_completed",
            user_id=order.user_id,
            idempotency_key=f"activation_completed:{order.id}",
            payload={"order_kind": order.order_kind},
        )
        if order.order_kind in {ORDER_KIND_EXTEND, ORDER_KIND_RESUME}:
            await ProductEventRepository(self._session).record(
                event_name="renewal_succeeded",
                user_id=order.user_id,
                idempotency_key=f"renewal_succeeded:{order.id}",
                payload={"order_kind": order.order_kind},
            )
        await NotificationOutboxRepository(self._session).enqueue_once(
            idempotency_key=f"order-activated:{order.public_order_id}",
            notification_kind="order_activated",
            user_id=order.user_id,
            subscription_id=subscription.id,
            order_id=order.id,
            bot_key=order.payment_bot_key or order.origin_bot_key,
            payload={
                "order_public_id": order.public_order_id,
                "operation_public_id": operation.public_id,
            },
        )
        await EntitlementOperationRepository(self._session).mark_completed(operation)
        await lease_repository.release(user_id=order.user_id, owner_key=owner_key)
        await self._session.commit()
        return ActivationOutcome(order_id=order.id, subscription=subscription, activated=True)

    async def activate_order_by_id(self, order_id: int) -> ActivationOutcome:
        order = await OrderRepository(self._session).get_by_id(order_id)

        if order is None:
            raise ValueError("Order was not found.")

        return await self.activate_order(order)

    async def retry_activation_by_id(self, order_id: int) -> ActivationOutcome:
        order = await OrderRepository(self._session).get_by_id(order_id)

        if order is None:
            raise ValueError("Order was not found.")

        if order.status == ORDER_STATUS_PAID:
            return await self.activate_order(order)

        if order.status not in ORDER_ACTIVATION_RETRY_STATUSES:
            raise ValueError("Only paid-but-unfinished orders can be retried.")

        return await self.activate_order(order)

    async def get_refund_candidate(self, order_id: int) -> RefundCandidate:
        order = await OrderRepository(self._session).get_by_id(order_id)

        if order is None:
            raise ValueError("Order was not found.")

        if order.status == ORDER_STATUS_REFUNDED:
            return RefundCandidate(
                order=order,
                is_eligible=False,
                already_refunded=True,
                reason="Order was already refunded.",
            )

        if order.status == ORDER_STATUS_REFUNDING:
            operation = await RefundOperationRepository(self._session).get_for_order(order.id)
            return RefundCandidate(
                order=order,
                is_eligible=False,
                reason="Refund is already in progress.",
                refund_operation_public_id=(operation.public_id if operation else None),
            )

        if (
            order.provider not in TELEGRAM_STARS_PROVIDERS
            or order.currency.upper() != TELEGRAM_STARS_CURRENCY
        ):
            return RefundCandidate(
                order=order,
                is_eligible=False,
                reason="Only Telegram Stars orders can be refunded with this command.",
            )

        if order.status not in ORDER_REFUNDABLE_STATUSES:
            return RefundCandidate(
                order=order,
                is_eligible=False,
                reason="Order is not in a refundable lifecycle state.",
            )

        if not order.provider_payment_id:
            return RefundCandidate(
                order=order,
                is_eligible=False,
                reason="Telegram Stars payment charge id is missing.",
            )

        application = await OrderApplicationRepository(self._session).get_for_order(order.id)
        if application is not None:
            has_later = await OrderApplicationRepository(self._session).has_later_application(
                application.subscription_id,
                application.applied_at_utc,
                order.id,
            )
            if has_later:
                return RefundCandidate(
                    order=order,
                    is_eligible=False,
                    reason=(
                        "A newer entitlement mutation exists; automatic compensation would "
                        "risk revoking unrelated paid access. Manual review is required."
                    ),
                )

        return RefundCandidate(order=order, is_eligible=True, charge_id=order.provider_payment_id)

    async def preview_refund(
        self,
        order_id: int,
        *,
        admin_telegram_id: int | None,
    ) -> RefundCandidate:
        candidate = await self.get_refund_candidate(order_id)
        if not candidate.is_eligible or candidate.charge_id is None:
            return candidate

        order = candidate.order
        application = await OrderApplicationRepository(self._session).get_for_order(order.id)
        subscription = None
        entitlement = None
        if application is not None:
            subscription = await SubscriptionRepository(self._session).get_by_id(
                application.subscription_id
            )
            if subscription is None:
                return RefundCandidate(
                    order=order,
                    is_eligible=False,
                    reason="Refund subscription is missing; manual review is required.",
                )
            entitlement = await EntitlementRepository(self._session).get_for_subscription(
                subscription.id
            )
            if entitlement is None:
                return RefundCandidate(
                    order=order,
                    is_eligible=False,
                    reason="Entitlement snapshot is missing; manual review is required.",
                )
            if entitlement.version != application.resulting_entitlement_version:
                return RefundCandidate(
                    order=order,
                    is_eligible=False,
                    reason=(
                        "The entitlement changed after this order; automatic refund is unsafe "
                        "and requires manual review."
                    ),
                )

        if application is None:
            previous_status = None
            previous_valid_until = None
            previous_max_devices = None
            target_status = ENTITLEMENT_STATUS_DISABLED
            target_valid_until = None
            target_max_devices = None
            expected_version = None
        elif order.order_kind == ORDER_KIND_PURCHASE:
            previous_status = application.previous_status or ENTITLEMENT_STATUS_DISABLED
            previous_valid_until = application.previous_valid_until_utc
            previous_max_devices = application.previous_max_devices
            target_status = ENTITLEMENT_STATUS_DISABLED
            target_valid_until = to_aware_utc(subscription.expires_at)
            target_max_devices = subscription.max_devices
            expected_version = entitlement.version
        else:
            previous_valid_until = (
                application.previous_valid_until_utc or order.base_valid_until_utc
            )
            previous_max_devices = application.previous_max_devices or order.base_max_devices
            if previous_valid_until is None or previous_max_devices is None:
                return RefundCandidate(
                    order=order,
                    is_eligible=False,
                    reason=(
                        "The legacy order has no complete before-snapshot. The provider refund "
                        "was not called; manual review is required."
                    ),
                )
            previous_valid_until = to_aware_utc(previous_valid_until)
            previous_status = application.previous_status or (
                ENTITLEMENT_STATUS_ACTIVE
                if previous_valid_until > utc_now()
                else ENTITLEMENT_STATUS_EXPIRED
            )
            target_status = previous_status
            target_valid_until = previous_valid_until
            target_max_devices = previous_max_devices
            expected_version = entitlement.version

        operation = await RefundOperationRepository(self._session).create_once(
            order=order,
            subscription_id=(subscription.id if subscription is not None else None),
            provider_charge_id=candidate.charge_id,
        )
        token = secrets.token_urlsafe(12)
        token_hash = hashlib.sha256(token.encode("utf-8")).hexdigest()
        expires_at = utc_now() + timedelta(
            minutes=max(self._settings.refund_confirmation_ttl_minutes, 1)
        )
        evidence = {
            "order_id": order.id,
            "order_kind": order.order_kind,
            "application_id": application.id if application is not None else None,
            "expected_version": expected_version,
            "previous_status": previous_status,
            "previous_valid_until": (
                previous_valid_until.isoformat() if previous_valid_until is not None else None
            ),
            "previous_max_devices": previous_max_devices,
            "target_status": target_status,
            "target_valid_until": (
                target_valid_until.isoformat() if target_valid_until is not None else None
            ),
            "target_max_devices": target_max_devices,
        }
        evidence_hash = hashlib.sha256(
            json.dumps(evidence, sort_keys=True, separators=(",", ":")).encode("utf-8")
        ).hexdigest()
        plan = await RefundPlanRepository(self._session).create_or_refresh(
            operation=operation,
            order=order,
            subscription_id=(subscription.id if subscription is not None else None),
            expected_current_entitlement_version=expected_version,
            previous_status=previous_status,
            previous_valid_until_utc=previous_valid_until,
            previous_max_devices=previous_max_devices,
            target_status=target_status,
            target_valid_until_utc=target_valid_until,
            target_max_devices=target_max_devices,
            evidence_hash=evidence_hash,
            confirmation_token_hash=token_hash,
            confirmation_expires_at_utc=expires_at,
            created_by_admin_telegram_id=admin_telegram_id,
        )
        referral = await ReferralRewardRepository(self._session).get_for_source_order(order.id)
        referral_action = None
        if referral is not None:
            referral_action = (
                "cancel"
                if referral.status in {"pending_hold", "available", "failed"}
                else "reverse"
            )
        await self._session.commit()
        return RefundCandidate(
            order=order,
            is_eligible=True,
            charge_id=candidate.charge_id,
            refund_operation_public_id=operation.public_id,
            confirmation_token=token,
            target_status=plan.target_status,
            target_valid_until_utc=plan.target_valid_until_utc,
            target_max_devices=plan.target_max_devices,
            referral_action=referral_action,
        )

    async def confirm_refund(
        self,
        confirmation_token: str,
        *,
        admin_telegram_id: int | None,
    ) -> RefundCandidate:
        token_hash = hashlib.sha256(confirmation_token.encode("utf-8")).hexdigest()
        plan_repository = RefundPlanRepository(self._session)
        plan = await plan_repository.get_by_confirmation_hash(token_hash)
        if plan is None:
            raise ValueError("refund_confirmation_invalid")
        if plan.state != "prepared":
            raise ValueError("refund_confirmation_already_used")
        if (
            plan.confirmation_expires_at_utc is None
            or to_aware_utc(plan.confirmation_expires_at_utc) <= utc_now()
        ):
            await plan_repository.mark_state(plan, "expired", "confirmation_expired")
            await self._session.commit()
            raise ValueError("refund_confirmation_expired")
        if (
            plan.created_by_admin_telegram_id is not None
            and admin_telegram_id is not None
            and plan.created_by_admin_telegram_id != admin_telegram_id
        ):
            raise ValueError("refund_confirmation_actor_mismatch")

        order = await OrderRepository(self._session).get_by_id(plan.order_id)
        operation = await RefundOperationRepository(self._session).get_for_order(plan.order_id)
        if order is None or operation is None or not order.provider_payment_id:
            raise ValueError("refund_operation_not_found")

        owner_key = f"refund:{order.public_order_id}"
        lease_repository = AccessOperationLeaseRepository(self._session)
        if not await lease_repository.acquire(
            user_id=order.user_id,
            owner_kind="refund",
            owner_key=owner_key,
            lease_seconds=3600,
        ):
            await self._session.rollback()
            raise ValueError("access_operation_in_progress")
        if plan.expected_current_entitlement_version is not None:
            entitlement = (
                await EntitlementRepository(self._session).get_for_subscription(
                    plan.subscription_id
                )
                if plan.subscription_id is not None
                else None
            )
            if (
                entitlement is None
                or entitlement.version != plan.expected_current_entitlement_version
            ):
                error_code = "required_entitlement_version_changed_before_provider"
                await plan_repository.mark_state(plan, "manual_review", error_code)
                await RefundOperationRepository(self._session).mark_manual_review(
                    operation, error_code
                )
                await lease_repository.release(
                    user_id=order.user_id,
                    owner_key=owner_key,
                )
                await self._session.commit()
                raise ValueError("refund_state_changed")
        transitioned = await OrderRepository(self._session).transition_status(
            order.id,
            list(ORDER_REFUNDABLE_STATUSES),
            ORDER_STATUS_REFUNDING,
        )
        if not transitioned and order.status != ORDER_STATUS_REFUNDING:
            await AccessOperationLeaseRepository(self._session).release(
                user_id=order.user_id,
                owner_key=owner_key,
            )
            raise ValueError("refund_state_changed")
        await ReferralRewardRepository(self._session).cancel_unapplied_for_source_order(order.id)
        await plan_repository.mark_confirmed(plan)
        await RefundOperationRepository(self._session).mark_provider_requested(operation)
        await NotificationOutboxRepository(self._session).enqueue_once(
            idempotency_key=f"refund-started:{order.public_order_id}",
            notification_kind="refund_started",
            user_id=order.user_id,
            subscription_id=operation.subscription_id,
            order_id=order.id,
            bot_key=order.payment_bot_key or order.origin_bot_key,
            payload={"order_public_id": order.public_order_id},
        )
        await self._session.commit()
        return RefundCandidate(
            order=order,
            is_eligible=True,
            charge_id=order.provider_payment_id,
            refund_operation_public_id=operation.public_id,
            target_status=plan.target_status,
            target_valid_until_utc=plan.target_valid_until_utc,
            target_max_devices=plan.target_max_devices,
        )

    async def prepare_refund(self, order_id: int) -> RefundCandidate:
        # Backward-compatible programmatic entry point. Human admin flows use preview + confirm.
        preview = await self.preview_refund(order_id, admin_telegram_id=None)
        if not preview.is_eligible or preview.confirmation_token is None:
            return preview
        return await self.confirm_refund(
            preview.confirmation_token,
            admin_telegram_id=None,
        )

    async def mark_refund_provider_unknown(self, order_id: int, error_code: str) -> None:
        operation = await RefundOperationRepository(self._session).get_for_order(order_id)
        order = await OrderRepository(self._session).get_by_id(order_id)
        if operation is None or order is None:
            return
        await RefundOperationRepository(self._session).mark_manual_review(operation, error_code)
        await NotificationOutboxRepository(self._session).enqueue_once(
            idempotency_key=f"refund-unknown:{operation.public_id}",
            notification_kind="operator_refund_unknown_alert",
            user_id=order.user_id,
            subscription_id=operation.subscription_id,
            order_id=order.id,
            payload={
                "operation_public_id": operation.public_id,
                "reason_code": error_code,
            },
        )
        await self._session.commit()

    async def complete_refund_after_provider(self, order_id: int) -> None:
        order = await OrderRepository(self._session).get_by_id(order_id)
        operation = await RefundOperationRepository(self._session).get_for_order(order_id)
        plan_repository = RefundPlanRepository(self._session)
        plan = await plan_repository.get_for_order(order_id)
        if order is None or operation is None:
            raise ValueError("refund_operation_not_found")
        if operation.state == "completed" and order.status == ORDER_STATUS_REFUNDED:
            return
        if order.status != ORDER_STATUS_REFUNDING:
            raise ValueError("refund_not_prepared")

        owner_key = f"refund:{order.public_order_id}"
        lease_repository = AccessOperationLeaseRepository(self._session)
        lease_owned = await lease_repository.renew(
            user_id=order.user_id,
            owner_key=owner_key,
            lease_seconds=3600,
        )
        if not lease_owned:
            lease_owned = await lease_repository.acquire(
                user_id=order.user_id,
                owner_kind="refund",
                owner_key=owner_key,
                lease_seconds=3600,
            )
        if not lease_owned:
            raise ValueError("access_operation_in_progress")
        refund_repository = RefundOperationRepository(self._session)
        await refund_repository.mark_provider_refunded(operation)
        if plan is None:
            await refund_repository.mark_manual_review(operation, "refund_plan_missing")
            await self._session.commit()
            raise RuntimeError("refund_plan_missing_after_provider_refund")
        await plan_repository.mark_state(plan, "provider_refunded")
        await self._session.commit()

        application = await OrderApplicationRepository(self._session).get_for_order(order.id)
        if application is not None:
            subscription = await SubscriptionRepository(self._session).get_by_id(
                application.subscription_id
            )
            if subscription is None:
                await refund_repository.mark_manual_review(operation, "refund_subscription_missing")
                await plan_repository.mark_state(
                    plan, "manual_review", "refund_subscription_missing"
                )
                await self._session.commit()
                raise RuntimeError("refund_subscription_missing")
            entitlement = await EntitlementRepository(self._session).get_for_subscription(
                subscription.id
            )
            if entitlement is None:
                await refund_repository.mark_manual_review(operation, "refund_entitlement_missing")
                await plan_repository.mark_state(
                    plan, "manual_review", "refund_entitlement_missing"
                )
                await self._session.commit()
                raise RuntimeError("refund_entitlement_missing")
            if (
                plan.expected_current_entitlement_version is not None
                and entitlement.version != plan.expected_current_entitlement_version
            ):
                await refund_repository.mark_manual_review(
                    operation, "required_entitlement_version_changed"
                )
                await plan_repository.mark_state(
                    plan, "manual_review", "required_entitlement_version_changed"
                )
                await self._session.commit()
                raise RuntimeError("refund_compensation_requires_manual_review")
            if plan.target_valid_until_utc is None or plan.target_max_devices is None:
                await refund_repository.mark_manual_review(operation, "refund_plan_incomplete")
                await plan_repository.mark_state(plan, "manual_review", "refund_plan_incomplete")
                await self._session.commit()
                raise RuntimeError("refund_plan_incomplete")

            entitlement_operation = await EntitlementOperationCoordinator(
                self._session, self._mediator_client
            ).prepare_generic(
                user_id=order.user_id,
                subscription_id=subscription.id,
                operation_type="refund_compensation",
                source_entity_type="refund",
                source_entity_id=operation.public_id,
                duration_delta_seconds=0,
                requested_device_limit=plan.target_max_devices,
                requested_status=plan.target_status,
                observed_valid_until_utc=subscription.expires_at,
            )
            await refund_repository.mark_compensation_pending(operation, entitlement_operation.id)
            await plan_repository.mark_state(plan, "compensation_pending")
            await self._session.commit()
            entitlement_operation = await EntitlementOperationRepository(
                self._session
            ).get_by_public_id(entitlement_operation.public_id)
            subscription = await SubscriptionRepository(self._session).get_by_id(subscription.id)
            if entitlement_operation is None or subscription is None:
                raise RuntimeError("refund_compensation_state_missing")
            applied = await EntitlementOperationCoordinator(
                self._session, self._mediator_client
            ).apply_generic(
                entitlement_operation,
                subscription,
                exact_valid_until_utc=to_aware_utc(plan.target_valid_until_utc),
                exact_device_limit=plan.target_max_devices,
                required_current_version=plan.expected_current_entitlement_version,
            )
            if applied is None:
                raise RuntimeError("refund_compensation_superseded")
            subscription.status = plan.target_status
            subscription.expires_at = applied.valid_until_utc
            subscription.max_devices = applied.max_device_tokens
            subscription.disabled_at = (
                utc_now() if plan.target_status == SUBSCRIPTION_STATUS_DISABLED else None
            )
            subscription.updated_at_utc = utc_now()
            await EntitlementRepository(self._session).set_authoritative(
                subscription,
                version=applied.version,
                status=applied.status,
                valid_until_utc=applied.valid_until_utc,
                max_device_tokens=applied.max_device_tokens,
            )
            await CommercialEntitlementAdjustmentRepository(self._session).mark_reversed_for_order(
                order.id
            )
            await CommercialEntitlementSegmentRepository(self._session).mark_reversed_for_order(
                order.id
            )
            await EntitlementOperationRepository(self._session).mark_completed(
                entitlement_operation
            )

        await DiscountRedemptionRepository(self._session).release_for_order(order.id)
        await OrderRepository(self._session).mark_refunded(order)
        await refund_repository.mark_completed(operation)
        await plan_repository.mark_state(plan, "completed")
        await NotificationOutboxRepository(self._session).enqueue_once(
            idempotency_key=f"refund-completed:{order.public_order_id}",
            notification_kind="refund_completed",
            user_id=order.user_id,
            subscription_id=operation.subscription_id,
            order_id=order.id,
            bot_key=order.payment_bot_key or order.origin_bot_key,
            payload={"order_public_id": order.public_order_id},
        )
        await AccessOperationLeaseRepository(self._session).release(
            user_id=order.user_id,
            owner_key=owner_key,
        )
        await self._session.commit()

    async def mark_order_refunded(self, order: Order) -> None:
        # Backward-compatible entry point for callers that have already obtained a provider
        # success. It still requires a prepared durable refund operation.
        await self.complete_refund_after_provider(order.id)

    async def _activate_subscription_for_order(
        self,
        order: Order,
        operation: EntitlementOperation,
    ) -> tuple[Subscription, EntitlementOperation]:
        subscription_repository = SubscriptionRepository(self._session)
        application_repository = OrderApplicationRepository(self._session)

        existing_application = await application_repository.get_for_order(order.id)
        if existing_application is not None:
            existing_subscription = await subscription_repository.get_by_id(
                existing_application.subscription_id
            )
            if existing_subscription is None:
                raise RuntimeError("Order application points to a missing subscription.")
            if operation.state != "completed":
                await EntitlementOperationRepository(self._session).mark_completed(operation)
            return existing_subscription, operation

        target_subscription = None
        if order.target_subscription_id is not None:
            target_subscription = await subscription_repository.get_by_id(
                order.target_subscription_id
            )
            if target_subscription is None or target_subscription.user_id != order.user_id:
                raise RuntimeError("Order target subscription is invalid.")
            if target_subscription.reconciliation_state != "healthy":
                raise MediatorClientError(
                    "Subscription is quarantined by reconciliation.",
                    error_code="reconciliation_blocked",
                )
        elif order.order_kind != ORDER_KIND_PURCHASE:
            raise RuntimeError("Non-purchase order has no target subscription.")

        selected_max_devices = (
            order.requested_max_devices
            or order.selected_max_devices
            or (order.tariff.max_devices if order.tariff is not None else None)
        )
        if selected_max_devices is None or selected_max_devices <= 0:
            raise RuntimeError("Order does not contain a valid device limit.")
        operation.requested_device_limit = selected_max_devices
        operation.duration_delta_seconds = (
            max(
                order.purchased_duration_days
                if order.purchased_duration_days is not None
                else (order.requested_duration_days or order.duration_days),
                0,
            )
            * 86400
        )

        previous_entitlement = (
            await EntitlementRepository(self._session).get_for_subscription(target_subscription.id)
            if target_subscription is not None
            else None
        )
        previous_entitlement_version = (
            previous_entitlement.version if previous_entitlement is not None else None
        )
        previous_status = (
            target_subscription.status
            if target_subscription is not None
            else ENTITLEMENT_STATUS_DISABLED
        )
        previous_valid_until = (
            to_aware_utc(target_subscription.expires_at)
            if target_subscription is not None
            else None
        )
        previous_max_devices = (
            target_subscription.max_devices if target_subscription is not None else None
        )

        coordinator = EntitlementOperationCoordinator(self._session, self._mediator_client)
        applied = await coordinator.apply_order(operation, order, target_subscription)
        entitlement_source_kind = (
            "paid_order" if order.amount_minor_units > 0 else "complimentary_order"
        )
        device_limit_before = target_subscription.max_devices if target_subscription else 0

        if target_subscription is None:
            subscription = await subscription_repository.create(
                user=order.user,
                tariff=order.tariff,
                public_guid=applied.public_guid,
                expires_at=applied.valid_until_utc,
                max_devices=applied.max_device_tokens,
            )
            operation.subscription_id = subscription.id
        else:
            subscription = target_subscription
            await subscription_repository.extend(
                subscription=subscription,
                tariff=order.tariff,
                new_expires_at=applied.valid_until_utc,
                max_devices=applied.max_device_tokens,
            )

        entitlement = await EntitlementRepository(self._session).set_authoritative(
            subscription,
            version=applied.version,
            status=applied.status,
            valid_until_utc=applied.valid_until_utc,
            max_device_tokens=applied.max_device_tokens,
        )
        await CommercialEntitlementAdjustmentRepository(self._session).create_applied_once(
            subscription=subscription,
            source_kind=entitlement_source_kind,
            duration_delta_seconds=operation.duration_delta_seconds,
            device_limit_before=device_limit_before,
            device_limit_after=applied.max_device_tokens,
            idempotency_key=f"order:{order.public_order_id}",
            source_order_id=order.id,
        )
        if operation.duration_delta_seconds > 0:
            await CommercialEntitlementSegmentRepository(self._session).create_applied_once(
                subscription_id=subscription.id,
                source_kind=entitlement_source_kind,
                starts_at_utc=(
                    applied.valid_until_utc - timedelta(seconds=operation.duration_delta_seconds)
                ),
                ends_at_utc=applied.valid_until_utc,
                idempotency_key=f"order:{order.public_order_id}",
                source_order_id=order.id,
                source_entity_id=str(order.id),
            )
        await application_repository.create(
            order,
            subscription,
            applied.valid_until_utc,
            entitlement.version,
            previous_entitlement_version=previous_entitlement_version,
            previous_status=previous_status,
            previous_valid_until_utc=previous_valid_until,
            previous_max_devices=previous_max_devices,
        )
        await self._mark_trial_converted_if_needed(order)
        await self._create_referral_reward_if_needed(order)
        return subscription, operation

    async def _subscription_for_applied_order(self, order: Order) -> Subscription:
        application = await OrderApplicationRepository(self._session).get_for_order(order.id)

        if application is None:
            raise RuntimeError("Paid order has no recorded subscription application.")

        subscription = await SubscriptionRepository(self._session).get_by_id(
            application.subscription_id
        )

        if subscription is None:
            raise RuntimeError("Order application points to a missing subscription.")

        return subscription

    def _calculate_new_expires_at(
        self,
        order: Order,
        target_subscription: Subscription | None,
    ) -> datetime:
        if order.order_kind == ORDER_KIND_UPGRADE_DEVICES:
            if target_subscription is None:
                raise RuntimeError("Upgrade order has no target subscription.")
            return to_aware_utc(target_subscription.expires_at)

        if order.target_expires_at_utc is not None:
            return to_aware_utc(order.target_expires_at_utc)

        purchased_duration_days = (
            order.purchased_duration_days
            if order.purchased_duration_days is not None
            else (order.requested_duration_days or order.duration_days)
        )
        expiration = calculate_expiration_snapshot(
            current_expires_at_utc=(
                order.base_valid_until_utc
                if order.base_valid_until_utc is not None
                else (target_subscription.expires_at if target_subscription is not None else None)
            ),
            captured_now_utc=order.created_at,
            purchased_duration_days=purchased_duration_days,
            order_kind=order.order_kind,
            business_timezone=self._settings.subscription_time_zone,
            configured_policy_version=self._settings.expiration_policy_version,
            policy_effective_at_utc=self._settings.expiration_policy_effective_at_utc,
        )
        order.base_expires_at_utc = expiration.base_expires_at_utc
        order.purchased_duration_days = expiration.purchased_duration_days
        order.expiration_policy_version = expiration.expiration_policy_version
        order.target_expires_at_utc = expiration.target_expires_at_utc
        return expiration.target_expires_at_utc

    async def _mark_trial_converted_if_needed(self, order: Order) -> None:
        if order.trial_claim_id is None:
            return

        claim = await TrialClaimRepository(self._session).get_for_user(order.user_id)

        if claim is None or claim.id != order.trial_claim_id:
            return

        if claim.status == TRIAL_STATUS_ACTIVE:
            claim.status = TRIAL_STATUS_CONVERTED
            claim.converted_at_utc = utc_now()

    async def _create_referral_reward_if_needed(self, order: Order) -> None:
        if not self._settings.referral_enabled:
            return
        policy = await CommercePolicyRepository(self._session).get()
        if not policy.referrals_enabled:
            return
        if not order.referral_eligible or order.amount_minor_units <= 0:
            return

        if order.order_kind not in ORDER_KINDS_WITH_PAID_DURATION:
            return

        if order.user.referred_by_user_id is None or order.user.referral_blocked:
            return

        duration_days = order.requested_duration_days or order.duration_days

        if duration_days <= 0:
            return

        reward_seconds = duration_days * 86400 * REFERRAL_REWARD_PERCENT // 100

        if reward_seconds <= 0:
            return

        await ReferralRewardRepository(self._session).create_once(
            referrer_user_id=order.user.referred_by_user_id,
            referred_user_id=order.user_id,
            source_order_id=order.id,
            reward_percent=REFERRAL_REWARD_PERCENT,
            reward_duration_seconds=reward_seconds,
            available_at_utc=utc_now() + timedelta(hours=self._settings.referral_reward_hold_hours),
        )

    def _mediator_order_note(self, order: Order) -> str:
        return f"order:{order.public_order_id}"

    def _order_amount_and_currency(self, tariff: Tariff) -> tuple[int, str]:
        currency = tariff.currency.upper()

        if self._settings.payment_mode != PAYMENT_MODE_TELEGRAM_STARS:
            return tariff.price_minor_units, currency

        if currency != TELEGRAM_STARS_CURRENCY:
            raise ValueError("Telegram Stars tariffs must use XTR currency.")

        return tariff.price_minor_units, TELEGRAM_STARS_CURRENCY


class SubscriptionService:
    def __init__(
        self,
        session: AsyncSession,
        settings: Settings,
        mediator_client: MediatorClient,
    ) -> None:
        self._session = session
        self._settings = settings
        self._mediator_client = mediator_client

    async def get_user_subscription(self, telegram_id: int) -> Subscription | None:
        user = await UserRepository(self._session).get_by_telegram_id(telegram_id)

        if user is None:
            return None

        return await SubscriptionRepository(self._session).get_primary_for_user(user)

    async def reset_devices(self, telegram_id: int) -> tuple[int, Subscription]:
        user = await UserRepository(self._session).get_by_telegram_id(telegram_id)

        if user is None:
            raise ValueError("User was not found.")

        subscription = await SubscriptionRepository(self._session).get_active_for_user(user.id)

        if subscription is None:
            raise ValueError("No active subscription was found.")

        reset_repository = DeviceResetRepository(self._session)
        can_reset, next_allowed_at = await reset_repository.can_reset(
            subscription_id=subscription.id,
            cooldown_hours=self._settings.device_reset_cooldown_hours,
        )

        if not can_reset:
            formatted = (
                next_allowed_at.strftime("%Y-%m-%d %H:%M UTC") if next_allowed_at else "later"
            )
            raise ValueError(f"Device reset is available after {formatted}.")

        unbound_devices = await self._mediator_client.revoke_all_device_tokens(
            subscription.public_guid
        )
        await reset_repository.add_reset_event(subscription, user)
        await OnboardingSessionRepository(
            self._session
        ).restart_open_device_issuance_for_subscription(
            user.id,
            subscription.id,
        )
        return unbound_devices, subscription

    async def list_device_tokens(self, telegram_id: int) -> list[DeviceTokenListItem]:
        user = await UserRepository(self._session).get_by_telegram_id(telegram_id)

        if user is None:
            raise ValueError("User was not found.")

        subscription = await SubscriptionRepository(self._session).get_primary_for_user(user)

        if subscription is None:
            raise ValueError("No subscription was found.")

        return await self._mediator_client.list_device_tokens(subscription.public_guid)

    async def revoke_device_token(self, telegram_id: int, device_public_id: str) -> None:
        user = await UserRepository(self._session).get_by_telegram_id(telegram_id)

        if user is None:
            raise ValueError("User was not found.")

        subscription = await SubscriptionRepository(self._session).get_primary_for_user(user)

        if subscription is None:
            raise ValueError("No subscription was found.")

        await self._mediator_client.revoke_device_token(
            subscription.public_guid,
            device_public_id,
        )

    async def get_mediator_details(self, subscription: Subscription) -> dict | None:
        try:
            details = await self._mediator_client.get_subscription(subscription.public_guid)
        except MediatorClientError:
            return None

        return {
            "active_device_count": details.active_device_count,
            "max_devices": details.max_devices,
            "is_active": details.is_active,
        }


@dataclass(frozen=True)
class TrialActivationOutcome:
    claim: TrialClaim | None
    subscription: Subscription | None
    activated: bool
    already_had_trial: bool = False
    failure_code: str | None = None
    eligibility_reason: TrialEligibilityReason | None = None

    @property
    def failure_message(self) -> str | None:
        return self.failure_code


class TrialService:
    def __init__(
        self,
        session: AsyncSession,
        settings: Settings,
        mediator_client: MediatorClient,
    ) -> None:
        self._session = session
        self._settings = settings
        self._mediator_client = mediator_client

    async def get_eligibility(
        self,
        telegram_id: int,
        username: str | None,
        first_name: str | None,
    ):
        user = await UserRepository(self._session).get_or_create_from_message_user(
            telegram_id=telegram_id,
            username=username,
            first_name=first_name,
        )
        subscription = await SubscriptionRepository(self._session).get_primary_for_user(user)
        return await TrialEligibilityService(self._session, self._settings).evaluate(
            user,
            subscription,
        )

    async def activate_trial(
        self,
        telegram_id: int,
        username: str | None,
        first_name: str | None,
    ) -> TrialActivationOutcome:
        user = await UserRepository(self._session).get_or_create_from_message_user(
            telegram_id=telegram_id,
            username=username,
            first_name=first_name,
        )
        claim_repository = TrialClaimRepository(self._session)
        subscription_repository = SubscriptionRepository(self._session)
        subscription = await subscription_repository.get_primary_for_user(user)
        eligibility = await TrialEligibilityService(self._session, self._settings).evaluate(
            user,
            subscription,
        )
        if not eligibility.is_available:
            claim = await claim_repository.get_for_user(user.id)
            existing_subscription = (
                await subscription_repository.get_by_id(claim.subscription_id)
                if claim is not None and claim.subscription_id is not None
                else subscription
            )
            return TrialActivationOutcome(
                claim=claim,
                subscription=existing_subscription,
                activated=claim is not None and claim.status == TRIAL_STATUS_ACTIVE,
                already_had_trial=True,
                eligibility_reason=eligibility.reason,
            )

        claim, acquired = await claim_repository.acquire_activation(
            user,
            duration_seconds=TRIAL_DURATION_SECONDS,
            max_devices=TRIAL_MAX_DEVICES,
        )
        if not acquired:
            refreshed_eligibility = await TrialEligibilityService(
                self._session,
                self._settings,
            ).evaluate(user, subscription)
            existing_subscription = (
                await subscription_repository.get_by_id(claim.subscription_id)
                if claim is not None and claim.subscription_id is not None
                else subscription
            )
            return TrialActivationOutcome(
                claim=claim,
                subscription=existing_subscription,
                activated=claim is not None and claim.status == TRIAL_STATUS_ACTIVE,
                already_had_trial=claim is not None,
                eligibility_reason=refreshed_eligibility.reason,
            )

        if claim is None:
            raise RuntimeError("trial_claim_missing_after_successful_acquisition")

        now = utc_now()
        claim.reserved_at_utc = claim.reserved_at_utc or claim.created_at_utc or now
        claim.status = TRIAL_STATUS_ACTIVATING
        claim.max_devices = TRIAL_MAX_DEVICES
        claim.duration_seconds = TRIAL_DURATION_SECONDS
        claim.activation_attempt_count += 1
        claim.last_activation_attempt_at_utc = now
        claim.failure_code = None
        owner_key = claim.idempotency_key
        lease_repository = AccessOperationLeaseRepository(self._session)
        lease_acquired = await lease_repository.acquire(
            user_id=user.id,
            owner_kind="trial",
            owner_key=owner_key,
        )
        if not lease_acquired:
            await self._session.rollback()
            claim = await claim_repository.get_for_user(user.id)
            return TrialActivationOutcome(
                claim=claim,
                subscription=subscription,
                activated=False,
                failure_code="access_operation_in_progress",
                eligibility_reason=TrialEligibilityReason.ACTIVATION_IN_PROGRESS,
            )

        coordinator = EntitlementOperationCoordinator(self._session, self._mediator_client)
        operation = await coordinator.prepare_generic(
            user_id=user.id,
            subscription_id=subscription.id if subscription is not None else None,
            operation_type="trial_activation",
            source_entity_type="trial_claim",
            source_entity_id=str(claim.id),
            duration_delta_seconds=TRIAL_DURATION_SECONDS,
            requested_device_limit=TRIAL_MAX_DEVICES,
            requested_status=ENTITLEMENT_STATUS_ACTIVE,
            observed_valid_until_utc=(
                subscription.expires_at if subscription is not None else None
            ),
        )
        await self._session.commit()

        claim = await claim_repository.get_for_user(user.id)
        operation = await EntitlementOperationRepository(self._session).get_by_public_id(
            operation.public_id
        )
        subscription = await subscription_repository.get_primary_for_user(user)
        if claim is None or operation is None:
            raise RuntimeError("trial_activation_state_disappeared")

        try:
            if subscription is None:
                applied = await coordinator.apply_new_subscription(
                    operation,
                    customer_reference=f"telegram:{user.telegram_id}",
                )
                subscription = await subscription_repository.create(
                    user=user,
                    tariff=None,
                    public_guid=applied.public_guid,
                    expires_at=applied.valid_until_utc,
                    max_devices=applied.max_device_tokens,
                )
                operation.subscription_id = subscription.id
            else:
                applied = await coordinator.apply_generic(operation, subscription)
                if applied is None:
                    raise RuntimeError("trial_activation_was_unexpectedly_superseded")
                await subscription_repository.extend(
                    subscription=subscription,
                    tariff=None,
                    new_expires_at=applied.valid_until_utc,
                    max_devices=applied.max_device_tokens,
                )

            claim.subscription_id = subscription.id
            claim.status = TRIAL_STATUS_ACTIVE
            claim.started_at_utc = applied.applied_at_utc
            claim.usable_started_at_utc = applied.applied_at_utc
            claim.ends_at_utc = applied.valid_until_utc
            claim.entitlement_version = applied.version
            claim.activated_at_utc = applied.applied_at_utc
            claim.failure_code = None
            await EntitlementRepository(self._session).set_authoritative(
                subscription=subscription,
                version=applied.version,
                status=applied.status,
                valid_until_utc=applied.valid_until_utc,
                max_device_tokens=applied.max_device_tokens,
            )
            await CommercialEntitlementAdjustmentRepository(self._session).create_applied_once(
                subscription=subscription,
                source_kind="trial",
                duration_delta_seconds=TRIAL_DURATION_SECONDS,
                device_limit_before=0,
                device_limit_after=applied.max_device_tokens,
                idempotency_key=claim.idempotency_key,
                source_entity_id=str(claim.id),
            )
            await CommercialEntitlementSegmentRepository(self._session).create_applied_once(
                subscription_id=subscription.id,
                source_kind="trial",
                starts_at_utc=applied.valid_until_utc - timedelta(seconds=TRIAL_DURATION_SECONDS),
                ends_at_utc=applied.valid_until_utc,
                idempotency_key=claim.idempotency_key,
                source_entity_id=str(claim.id),
            )
            await ProductEventRepository(self._session).record(
                event_name="trial_activated",
                user_id=user.id,
                idempotency_key=f"trial_activated:{claim.id}",
            )
            await NotificationOutboxRepository(self._session).enqueue_once(
                idempotency_key=f"trial_activated:{claim.id}",
                notification_kind="trial_activated",
                user_id=user.id,
                subscription_id=subscription.id,
                payload={"trial_claim_id": claim.id},
            )
            await EntitlementOperationRepository(self._session).mark_completed(operation)
            await lease_repository.release(user_id=user.id, owner_key=owner_key)
            await self._session.commit()
            return TrialActivationOutcome(
                claim=claim,
                subscription=subscription,
                activated=True,
                eligibility_reason=TrialEligibilityReason.AVAILABLE,
            )
        except MediatorClientError as exception:
            failure_code = exception.error_code or "mediator_unavailable"
            await self._session.rollback()
            failed_claim = await TrialClaimRepository(self._session).get_for_user(user.id)
            if failed_claim is not None:
                failed_claim.status = TRIAL_STATUS_ACTIVATION_FAILED
                failed_claim.failure_code = failure_code
            await AccessOperationLeaseRepository(self._session).release(
                user_id=user.id,
                owner_key=owner_key,
            )
            await self._session.commit()
            return TrialActivationOutcome(
                claim=failed_claim,
                subscription=subscription,
                activated=False,
                failure_code=failure_code,
                eligibility_reason=TrialEligibilityReason.RETRY_FAILED_ACTIVATION,
            )
        except Exception:
            logger.exception("Trial activation finalization failed: user_id=%s", user.id)
            await self._session.rollback()
            failed_claim = await TrialClaimRepository(self._session).get_for_user(user.id)
            failed_operation = await EntitlementOperationRepository(self._session).get_by_public_id(
                operation.public_id
            )
            if failed_claim is not None:
                failed_claim.status = TRIAL_STATUS_ACTIVATION_FAILED
                failed_claim.failure_code = "activation_finalize_failed"
            if failed_operation is not None and failed_operation.state == "external_applied":
                failed_operation.state = "local_commit_pending"
                failed_operation.last_error_code = "activation_finalize_failed"
                failed_operation.updated_at_utc = utc_now()
            await AccessOperationLeaseRepository(self._session).release(
                user_id=user.id,
                owner_key=owner_key,
            )
            await self._session.commit()
            return TrialActivationOutcome(
                claim=failed_claim,
                subscription=subscription,
                activated=False,
                failure_code="activation_finalize_failed",
                eligibility_reason=TrialEligibilityReason.RETRY_FAILED_ACTIVATION,
            )


class AdminEntitlementAdjustmentService:
    def __init__(self, session: AsyncSession, mediator_client: MediatorClient) -> None:
        self._session = session
        self._mediator_client = mediator_client

    async def apply(
        self,
        *,
        public_guid: str,
        actor_telegram_id: int,
        source_request_id: str,
        reason: str,
        duration_days: int = 0,
        requested_device_limit: int | None = None,
        disable: bool = False,
    ) -> AdminEntitlementAdjustmentOutcome:
        normalized_reason = reason.strip()
        if not normalized_reason or len(normalized_reason) > 64:
            raise ValueError("admin_adjustment_reason_invalid")
        if duration_days < 0 or duration_days > 3650:
            raise ValueError("admin_adjustment_duration_invalid")

        repository = SubscriptionRepository(self._session)
        subscription = await repository.get_by_public_guid(public_guid)
        if subscription is None:
            raise ValueError("subscription_not_found")
        if subscription.reconciliation_state != "healthy":
            raise ValueError("reconciliation_blocked")
        if (
            not disable
            and duration_days == 0
            and (
                requested_device_limit is None or requested_device_limit <= subscription.max_devices
            )
        ):
            raise ValueError("admin_adjustment_has_no_monotonic_effect")
        if requested_device_limit is not None and requested_device_limit < subscription.max_devices:
            raise ValueError("admin_adjustment_cannot_reduce_device_limit")

        owner_key = f"admin-adjustment:{source_request_id}"
        lease = AccessOperationLeaseRepository(self._session)
        if not await lease.acquire(
            user_id=subscription.user_id,
            owner_kind="admin_adjustment",
            owner_key=owner_key,
            lease_seconds=300,
        ):
            raise ValueError("access_operation_in_progress")

        operation_type = "admin_revoke" if disable else "admin_adjustment"
        operation = await EntitlementOperationCoordinator(
            self._session, self._mediator_client
        ).prepare_generic(
            user_id=subscription.user_id,
            subscription_id=subscription.id,
            operation_type=operation_type,
            source_entity_type="admin_command",
            source_entity_id=source_request_id,
            duration_delta_seconds=duration_days * 86400,
            requested_device_limit=(
                requested_device_limit
                if requested_device_limit is not None
                else subscription.max_devices
            ),
            requested_status=(
                ENTITLEMENT_STATUS_DISABLED if disable else ENTITLEMENT_STATUS_ACTIVE
            ),
            observed_valid_until_utc=subscription.expires_at,
        )
        self._session.add(
            AuditEvent(
                created_at_utc=utc_now(),
                event_type="entitlement.admin_adjustment_prepared",
                telegram_id=actor_telegram_id,
                subscription_id=subscription.id,
                public_guid=subscription.public_guid,
                details_json=json.dumps(
                    {
                        "operation_public_id": operation.public_id,
                        "operation_type": operation_type,
                        "duration_days": duration_days,
                        "requested_device_limit": requested_device_limit,
                        "reason": normalized_reason,
                    },
                    ensure_ascii=False,
                    sort_keys=True,
                ),
            )
        )
        await self._session.commit()
        return await self.recover(operation.public_id, owner_key=owner_key)

    async def recover(
        self,
        operation_public_id: str,
        *,
        owner_key: str | None = None,
    ) -> AdminEntitlementAdjustmentOutcome:
        operation = await EntitlementOperationRepository(self._session).get_by_public_id(
            operation_public_id
        )
        if operation is None or operation.source_entity_type != "admin_command":
            raise ValueError("admin_adjustment_operation_not_found")
        if operation.subscription_id is None:
            raise RuntimeError("admin_adjustment_subscription_missing")
        subscription = await SubscriptionRepository(self._session).get_by_id(
            operation.subscription_id
        )
        if subscription is None:
            await EntitlementOperationRepository(self._session).mark_manual_review(
                operation, "admin_adjustment_subscription_missing"
            )
            await self._session.commit()
            raise RuntimeError("admin_adjustment_subscription_missing")

        resolved_owner_key = owner_key or f"admin-adjustment:{operation.source_entity_id}"
        lease_repository = AccessOperationLeaseRepository(self._session)
        owns_lease = await lease_repository.renew(
            user_id=subscription.user_id,
            owner_key=resolved_owner_key,
            lease_seconds=300,
        )
        if not owns_lease:
            owns_lease = await lease_repository.acquire(
                user_id=subscription.user_id,
                owner_kind="admin_adjustment",
                owner_key=resolved_owner_key,
                lease_seconds=300,
            )
        if not owns_lease:
            raise ValueError("access_operation_in_progress")

        if operation.state == "completed":
            entitlement = await EntitlementRepository(self._session).get_for_subscription(
                subscription.id
            )
            if entitlement is None:
                raise RuntimeError("admin_adjustment_entitlement_missing")
            await AccessOperationLeaseRepository(self._session).release(
                user_id=subscription.user_id,
                owner_key=resolved_owner_key,
            )
            await self._session.commit()
            return AdminEntitlementAdjustmentOutcome(
                operation.public_id,
                subscription,
                entitlement.version,
                entitlement.status,
            )

        applied = await EntitlementOperationCoordinator(
            self._session, self._mediator_client, worker_id="admin-adjustment-recovery"
        ).apply_generic(
            operation,
            subscription,
            exact_valid_until_utc=(
                subscription.expires_at if operation.operation_type == "admin_revoke" else None
            ),
        )
        if applied is None:
            raise RuntimeError("admin_adjustment_superseded")

        previous_expiry = to_aware_utc(subscription.expires_at)
        previous_limit = subscription.max_devices
        subscription.expires_at = applied.valid_until_utc
        subscription.max_devices = applied.max_device_tokens
        subscription.status = (
            SUBSCRIPTION_STATUS_DISABLED
            if applied.status == ENTITLEMENT_STATUS_DISABLED
            else SUBSCRIPTION_STATUS_ACTIVE
        )
        subscription.disabled_at = (
            utc_now() if applied.status == ENTITLEMENT_STATUS_DISABLED else None
        )
        subscription.updated_at_utc = utc_now()
        await EntitlementRepository(self._session).set_authoritative(
            subscription,
            version=applied.version,
            status=applied.status,
            valid_until_utc=applied.valid_until_utc,
            max_device_tokens=applied.max_device_tokens,
        )
        if applied.status == ENTITLEMENT_STATUS_DISABLED:
            await TrialClaimRepository(self._session).mark_terminal_for_subscription(
                subscription.id,
                terminal_status=TRIAL_STATUS_REVOKED,
                occurred_at_utc=utc_now(),
            )
        if operation.operation_type == "admin_adjustment":
            await CommercialEntitlementAdjustmentRepository(self._session).create_applied_once(
                subscription=subscription,
                source_kind="admin_adjustment",
                duration_delta_seconds=max(
                    int((applied.valid_until_utc - previous_expiry).total_seconds()), 0
                ),
                device_limit_before=previous_limit,
                device_limit_after=applied.max_device_tokens,
                idempotency_key=f"admin-adjustment:{operation.public_id}",
                source_entity_id=operation.source_entity_id,
            )
        await NotificationOutboxRepository(self._session).enqueue_once(
            idempotency_key=f"admin-adjustment-completed:{operation.public_id}",
            notification_kind=(
                "admin_subscription_revoked"
                if applied.status == ENTITLEMENT_STATUS_DISABLED
                else "admin_subscription_adjusted"
            ),
            user_id=subscription.user_id,
            subscription_id=subscription.id,
            payload={"operation_public_id": operation.public_id},
        )
        self._session.add(
            AuditEvent(
                created_at_utc=utc_now(),
                event_type="entitlement.admin_adjustment_completed",
                subscription_id=subscription.id,
                public_guid=subscription.public_guid,
                details_json=json.dumps(
                    {
                        "operation_public_id": operation.public_id,
                        "result_version": applied.version,
                        "result_status": applied.status,
                    },
                    sort_keys=True,
                ),
            )
        )
        await EntitlementOperationRepository(self._session).mark_completed(operation)
        await AccessOperationLeaseRepository(self._session).release(
            user_id=subscription.user_id,
            owner_key=resolved_owner_key,
        )
        await self._session.commit()
        return AdminEntitlementAdjustmentOutcome(
            operation.public_id, subscription, applied.version, applied.status
        )


class ReconciliationRepairService:
    _allowed_modes: ClassVar[frozenset[str]] = frozenset(
        {"adopt_remote", "adopt_expired", "adopt_disabled", "restore_local"}
    )

    def __init__(self, session: AsyncSession, mediator_client: MediatorClient) -> None:
        self._session = session
        self._mediator_client = mediator_client

    async def apply(
        self,
        *,
        public_guid: str,
        actor_telegram_id: int,
        source_request_id: str,
        reason: str,
        mode: str,
        expected_remote_version: int | None = None,
    ) -> ReconciliationRepairOutcome:
        normalized_reason = reason.strip()
        if mode not in self._allowed_modes:
            raise ValueError("reconciliation_repair_mode_invalid")
        if not normalized_reason or len(normalized_reason) > 128:
            raise ValueError("reconciliation_repair_reason_invalid")
        if mode in {"adopt_expired", "adopt_disabled"} and expected_remote_version is None:
            raise ValueError("reconciliation_expected_remote_version_required")

        subscription = await SubscriptionRepository(self._session).get_by_public_guid(public_guid)
        if subscription is None:
            raise ValueError("subscription_not_found")

        operation_type = f"reconciliation_{mode}"
        local = await EntitlementRepository(self._session).get_for_subscription(subscription.id)
        if local is None:
            raise RuntimeError("reconciliation_local_entitlement_missing")

        snapshot = None
        repair_source_id = source_request_id
        if mode in {"adopt_remote", "adopt_expired", "adopt_disabled"}:
            snapshot = await self._read_remote_snapshot(subscription.public_guid)
            if expected_remote_version is not None and snapshot.version != expected_remote_version:
                raise ValueError("reconciliation_snapshot_changed")
            repair_source_id = f"{subscription.id}:{mode}:remote-v{snapshot.version}"

        existing = await EntitlementOperationRepository(self._session).get_for_source(
            source_entity_type="reconciliation_repair",
            source_entity_id=repair_source_id,
            operation_type=operation_type,
        )
        if existing is not None:
            if snapshot is not None and not self._operation_snapshot_matches(existing, snapshot):
                raise ValueError("reconciliation_snapshot_changed")
            return await self.recover(existing.public_id)

        if subscription.reconciliation_state not in {"blocked", "recovering"}:
            raise ValueError("subscription_is_not_quarantined")

        operation_repository = EntitlementOperationRepository(self._session)
        if await operation_repository.has_active_for_subscription(subscription.id):
            raise ValueError("reconciliation_active_operation_exists")
        if await OrderRepository(self._session).has_unfinished_for_subscription(subscription.id):
            raise ValueError("reconciliation_unfinished_order_exists")

        if snapshot is not None:
            if mode == "adopt_expired":
                self._validate_legacy_expiration(subscription, local, snapshot)
            if mode == "adopt_disabled" and snapshot.status != ENTITLEMENT_STATUS_DISABLED:
                raise ValueError("reconciliation_remote_status_not_disabled")
            project_reconciled_lifecycle(
                current_subscription_status=subscription.status,
                entitlement_status=snapshot.status,
                valid_until_utc=snapshot.valid_until_utc,
                repair_mode=mode,
                now_utc=utc_now(),
            )
        else:
            project_reconciled_lifecycle(
                current_subscription_status=subscription.status,
                entitlement_status=local.status,
                valid_until_utc=local.valid_until_utc,
                repair_mode=mode,
                now_utc=utc_now(),
            )

        owner_key = f"reconciliation-repair:{repair_source_id}"
        lease = AccessOperationLeaseRepository(self._session)
        if not await lease.acquire(
            user_id=subscription.user_id,
            owner_kind="reconciliation_repair",
            owner_key=owner_key,
            lease_seconds=300,
        ):
            raise ValueError("access_operation_in_progress")

        try:
            requested_status = snapshot.status if snapshot is not None else local.status
            requested_limit = (
                snapshot.max_device_tokens if snapshot is not None else local.max_device_tokens
            )
            observed_until = (
                snapshot.valid_until_utc if snapshot is not None else local.valid_until_utc
            )
            operation = await EntitlementOperationCoordinator(
                self._session, self._mediator_client
            ).prepare_generic(
                user_id=subscription.user_id,
                subscription_id=subscription.id,
                operation_type=operation_type,
                source_entity_type="reconciliation_repair",
                source_entity_id=repair_source_id,
                duration_delta_seconds=0,
                requested_device_limit=requested_limit,
                requested_status=requested_status,
                observed_valid_until_utc=observed_until,
            )
            if snapshot is not None:
                operation.expected_version = snapshot.version
            self._session.add(
                AuditEvent(
                    created_at_utc=utc_now(),
                    event_type="entitlement.reconciliation_repair_prepared",
                    telegram_id=actor_telegram_id,
                    subscription_id=subscription.id,
                    public_guid=subscription.public_guid,
                    details_json=json.dumps(
                        {
                            "mode": mode,
                            "operation_public_id": operation.public_id,
                            "source_request_id": source_request_id,
                            "reason": normalized_reason,
                            "expected_remote_version": expected_remote_version,
                            "reconciliation_reason": subscription.reconciliation_reason,
                            "subscription_status": subscription.status,
                            "local_version": local.version,
                            "local_status": local.status,
                            "local_valid_until_utc": to_aware_utc(
                                local.valid_until_utc
                            ).isoformat(),
                            "local_max_device_tokens": local.max_device_tokens,
                            "remote_snapshot": (
                                {
                                    "version": snapshot.version,
                                    "status": snapshot.status,
                                    "valid_until_utc": snapshot.valid_until_utc.isoformat(),
                                    "max_device_tokens": snapshot.max_device_tokens,
                                }
                                if snapshot is not None
                                else None
                            ),
                        },
                        ensure_ascii=False,
                        sort_keys=True,
                    ),
                )
            )
            await self._session.commit()
            return await self.recover(operation.public_id, owner_key=owner_key)
        except Exception:
            await self._session.rollback()
            await AccessOperationLeaseRepository(self._session).release(
                user_id=subscription.user_id,
                owner_key=owner_key,
            )
            await self._session.commit()
            raise

    async def recover(
        self,
        operation_public_id: str,
        *,
        owner_key: str | None = None,
    ) -> ReconciliationRepairOutcome:
        operation = await EntitlementOperationRepository(self._session).get_by_public_id(
            operation_public_id
        )
        if operation is None or operation.source_entity_type != "reconciliation_repair":
            raise ValueError("reconciliation_repair_operation_not_found")
        if operation.operation_type not in {
            "reconciliation_adopt_remote",
            "reconciliation_adopt_expired",
            "reconciliation_adopt_disabled",
            "reconciliation_restore_local",
        }:
            raise ValueError("reconciliation_repair_operation_type_invalid")
        if operation.subscription_id is None:
            raise RuntimeError("reconciliation_repair_subscription_missing")

        subscription = await SubscriptionRepository(self._session).get_by_id(
            operation.subscription_id
        )
        local = await EntitlementRepository(self._session).get_for_subscription(
            operation.subscription_id
        )
        if subscription is None or local is None:
            await EntitlementOperationRepository(self._session).mark_manual_review(
                operation, "reconciliation_repair_local_state_missing"
            )
            await self._session.commit()
            raise RuntimeError("reconciliation_repair_local_state_missing")

        mode_by_operation = {
            "reconciliation_adopt_remote": "adopt_remote",
            "reconciliation_adopt_expired": "adopt_expired",
            "reconciliation_adopt_disabled": "adopt_disabled",
            "reconciliation_restore_local": "restore_local",
        }
        mode = mode_by_operation[operation.operation_type]
        resolved_owner_key = owner_key or f"reconciliation-repair:{operation.source_entity_id}"
        lease_repository = AccessOperationLeaseRepository(self._session)
        owns_lease = await lease_repository.renew(
            user_id=subscription.user_id,
            owner_key=resolved_owner_key,
            lease_seconds=300,
        )
        if not owns_lease:
            owns_lease = await lease_repository.acquire(
                user_id=subscription.user_id,
                owner_kind="reconciliation_repair",
                owner_key=resolved_owner_key,
                lease_seconds=300,
            )
        if not owns_lease:
            raise ValueError("access_operation_in_progress")

        if operation.state == "completed":
            await lease_repository.release(
                user_id=subscription.user_id,
                owner_key=resolved_owner_key,
            )
            await self._session.commit()
            return ReconciliationRepairOutcome(
                operation.public_id,
                subscription,
                local.version,
                local.status,
                mode,
            )

        if mode in {"adopt_remote", "adopt_expired", "adopt_disabled"}:
            applied = await self._adopt_remote_result(operation, subscription)
            if mode == "adopt_expired":
                self._validate_legacy_expiration(subscription, local, applied)
        else:
            project_reconciled_lifecycle(
                current_subscription_status=subscription.status,
                entitlement_status=local.status,
                valid_until_utc=local.valid_until_utc,
                repair_mode=mode,
                now_utc=utc_now(),
            )
            applied = await EntitlementOperationCoordinator(
                self._session,
                self._mediator_client,
                worker_id="reconciliation-repair",
            ).apply_generic(
                operation,
                subscription,
                exact_valid_until_utc=to_aware_utc(local.valid_until_utc),
            )
            if applied is None:
                raise RuntimeError("reconciliation_restore_local_superseded")

        projection = project_reconciled_lifecycle(
            current_subscription_status=subscription.status,
            entitlement_status=applied.status,
            valid_until_utc=applied.valid_until_utc,
            repair_mode=mode,
            now_utc=utc_now(),
        )
        previous = {
            "subscription_status": subscription.status,
            "version": local.version,
            "status": local.status,
            "valid_until_utc": to_aware_utc(local.valid_until_utc).isoformat(),
            "max_device_tokens": local.max_device_tokens,
            "reconciliation_reason": subscription.reconciliation_reason,
        }
        subscription.expires_at = applied.valid_until_utc
        subscription.max_devices = applied.max_device_tokens
        subscription.status = projection.subscription_status
        subscription.disabled_at = projection.disabled_at_utc
        subscription.updated_at_utc = utc_now()
        subscription.reconciliation_state = "healthy"
        subscription.reconciliation_reason = None
        subscription.reconciliation_blocked_at_utc = None
        await EntitlementRepository(self._session).set_authoritative(
            subscription,
            version=applied.version,
            status=applied.status,
            valid_until_utc=applied.valid_until_utc,
            max_device_tokens=applied.max_device_tokens,
        )
        if projection.subscription_status == SUBSCRIPTION_STATUS_EXPIRED:
            await TrialClaimRepository(self._session).mark_terminal_for_subscription(
                subscription.id,
                terminal_status=TRIAL_STATUS_EXPIRED,
                occurred_at_utc=utc_now(),
            )
        elif applied.status == ENTITLEMENT_STATUS_DISABLED:
            await TrialClaimRepository(self._session).mark_terminal_for_subscription(
                subscription.id,
                terminal_status=TRIAL_STATUS_REVOKED,
                occurred_at_utc=utc_now(),
            )
        await NotificationOutboxRepository(self._session).enqueue_once(
            idempotency_key=f"reconciliation-repaired:{operation.public_id}",
            notification_kind="operator_reconciliation_repaired",
            user_id=subscription.user_id,
            subscription_id=subscription.id,
            payload={
                "mode": mode,
                "operation_public_id": operation.public_id,
                "public_guid": subscription.public_guid,
                "subscription_status": subscription.status,
                "entitlement_status": applied.status,
                "entitlement_version": applied.version,
                "lifecycle_reason_code": projection.reason_code,
            },
        )
        self._session.add(
            AuditEvent(
                created_at_utc=utc_now(),
                event_type="entitlement.reconciliation_repair_completed",
                subscription_id=subscription.id,
                public_guid=subscription.public_guid,
                details_json=json.dumps(
                    {
                        "mode": mode,
                        "operation_public_id": operation.public_id,
                        "before": previous,
                        "after": {
                            "subscription_status": subscription.status,
                            "version": applied.version,
                            "status": applied.status,
                            "valid_until_utc": applied.valid_until_utc.isoformat(),
                            "max_device_tokens": applied.max_device_tokens,
                            "lifecycle_reason_code": projection.reason_code,
                        },
                    },
                    sort_keys=True,
                ),
            )
        )
        await EntitlementOperationRepository(self._session).mark_completed(operation)
        await lease_repository.release(
            user_id=subscription.user_id,
            owner_key=resolved_owner_key,
        )
        await self._session.commit()
        return ReconciliationRepairOutcome(
            operation.public_id,
            subscription,
            applied.version,
            applied.status,
            mode,
        )

    async def _adopt_remote_result(
        self,
        operation: EntitlementOperation,
        subscription: Subscription,
    ) -> AppliedEntitlement:
        if operation.state in {"external_applied", "local_commit_pending"}:
            return self._from_operation(operation, subscription.public_guid)

        remote = await self._read_remote_snapshot(subscription.public_guid)
        if not self._operation_snapshot_matches(operation, remote):
            await EntitlementOperationRepository(self._session).mark_manual_review(
                operation, "reconciliation_snapshot_changed"
            )
            await self._session.commit()
            raise ValueError("reconciliation_snapshot_changed")

        await EntitlementOperationRepository(self._session).mark_external_applied(
            operation,
            result_version=remote.version,
            result_status=remote.status,
            result_valid_until_utc=remote.valid_until_utc,
            result_device_limit=remote.max_device_tokens,
        )
        await self._session.commit()
        return self._from_operation(operation, subscription.public_guid)

    @staticmethod
    def _operation_snapshot_matches(
        operation: EntitlementOperation,
        remote: AppliedEntitlement,
    ) -> bool:
        return (
            operation.expected_version == remote.version
            and operation.requested_status == remote.status
            and operation.requested_device_limit == remote.max_device_tokens
            and operation.observed_valid_until_utc is not None
            and to_aware_utc(operation.observed_valid_until_utc) == remote.valid_until_utc
        )

    async def _read_remote_snapshot(self, public_guid: str) -> AppliedEntitlement:
        remote = await self._mediator_client.get_entitlement(public_guid)
        if remote.valid_until_utc is None:
            raise MediatorClientError(
                "Mediator entitlement has no validity timestamp.",
                error_code="invalid_response",
            )
        valid_until = to_aware_utc(
            datetime.fromisoformat(remote.valid_until_utc.replace("Z", "+00:00"))
        )
        return AppliedEntitlement(
            public_guid=public_guid,
            version=remote.version,
            status=remote.status,
            valid_until_utc=valid_until,
            max_device_tokens=remote.max_device_tokens,
            applied_at_utc=utc_now(),
        )

    @staticmethod
    def _validate_legacy_expiration(
        subscription: Subscription,
        local,
        remote: AppliedEntitlement,
    ) -> None:
        expires_at = to_aware_utc(subscription.expires_at)
        local_until = to_aware_utc(local.valid_until_utc)
        if subscription.status != SUBSCRIPTION_STATUS_EXPIRED:
            raise ValueError("legacy_expiration_requires_expired_subscription")
        if local.status != ENTITLEMENT_STATUS_ACTIVE:
            raise ValueError("legacy_expiration_requires_local_active_entitlement")
        if remote.status != ENTITLEMENT_STATUS_DISABLED:
            raise ValueError("legacy_expiration_requires_remote_disabled_entitlement")
        if remote.version != local.version + 1:
            raise ValueError("legacy_expiration_version_mismatch")
        if not (expires_at == local_until == remote.valid_until_utc):
            raise ValueError("legacy_expiration_validity_mismatch")
        if local.max_device_tokens != remote.max_device_tokens:
            raise ValueError("legacy_expiration_device_limit_mismatch")
        if expires_at > utc_now():
            raise ValueError("legacy_expiration_has_future_validity")

    @staticmethod
    def _from_operation(
        operation: EntitlementOperation,
        public_guid: str,
    ) -> AppliedEntitlement:
        if (
            operation.external_result_version is None
            or operation.external_result_status is None
            or operation.external_result_valid_until_utc is None
            or operation.external_result_device_limit is None
        ):
            raise RuntimeError("reconciliation_repair_operation_result_incomplete")
        return AppliedEntitlement(
            public_guid=public_guid,
            version=operation.external_result_version,
            status=operation.external_result_status,
            valid_until_utc=to_aware_utc(operation.external_result_valid_until_utc),
            max_device_tokens=operation.external_result_device_limit,
            applied_at_utc=to_aware_utc(operation.external_applied_at_utc or utc_now()),
        )


class ExpirationService:
    def __init__(
        self,
        session: AsyncSession,
        mediator_client: MediatorClient,
    ) -> None:
        self._session = session
        self._mediator_client = mediator_client

    async def expire_due_subscriptions(self) -> int:
        subscription_repository = SubscriptionRepository(self._session)
        expired_subscriptions = await subscription_repository.list_expired_active()
        expired_count = 0

        for candidate in expired_subscriptions:
            subscription = await subscription_repository.get_by_id(candidate.id)
            if subscription is None or subscription.status != SUBSCRIPTION_STATUS_ACTIVE:
                continue
            observed_expiry = to_aware_utc(subscription.expires_at)
            if observed_expiry > utc_now():
                continue
            owner_key = f"expiration:{subscription.id}:{observed_expiry.isoformat()}"
            lease_repository = AccessOperationLeaseRepository(self._session)
            if not await lease_repository.acquire(
                user_id=subscription.user_id,
                owner_kind="expiration",
                owner_key=owner_key,
            ):
                continue
            try:
                coordinator = EntitlementOperationCoordinator(self._session, self._mediator_client)
                operation = await coordinator.prepare_generic(
                    user_id=subscription.user_id,
                    subscription_id=subscription.id,
                    operation_type="expiration",
                    source_entity_type="subscription_expiry",
                    source_entity_id=f"{subscription.id}:{observed_expiry.isoformat()}",
                    duration_delta_seconds=0,
                    requested_device_limit=subscription.max_devices,
                    requested_status=ENTITLEMENT_STATUS_EXPIRED,
                    observed_valid_until_utc=observed_expiry,
                )
                await self._session.commit()
                operation = await EntitlementOperationRepository(self._session).get_by_public_id(
                    operation.public_id
                )
                subscription = await subscription_repository.get_by_id(subscription.id)
                if operation is None or subscription is None:
                    raise RuntimeError("expiration_operation_disappeared")
                applied = await coordinator.apply_generic(
                    operation,
                    subscription,
                    exact_valid_until_utc=observed_expiry,
                    allow_stale_observation_supersede=True,
                )
                if applied is None:
                    await lease_repository.release(
                        user_id=subscription.user_id, owner_key=owner_key
                    )
                    await self._session.commit()
                    continue

                # Re-read after the external operation. A concurrent renewal that advanced
                # local state supersedes this stale expiration instead of being disabled.
                subscription = await subscription_repository.get_by_id(subscription.id)
                operation = await EntitlementOperationRepository(self._session).get_by_public_id(
                    operation.public_id
                )
                if subscription is None or operation is None:
                    raise RuntimeError("expiration_finalize_state_missing")
                if to_aware_utc(subscription.expires_at) > observed_expiry:
                    await EntitlementOperationRepository(self._session).mark_manual_review(
                        operation, "renewal_advanced_after_expiration_apply"
                    )
                    subscription.reconciliation_state = "blocked"
                    subscription.reconciliation_reason = "renewal_advanced_after_expiration_apply"
                    subscription.reconciliation_blocked_at_utc = utc_now()
                    await NotificationOutboxRepository(self._session).enqueue_once(
                        idempotency_key=f"expiration-race:{operation.public_id}",
                        notification_kind="operator_reconciliation_alert",
                        subscription_id=subscription.id,
                        user_id=subscription.user_id,
                        payload={
                            "operation_public_id": operation.public_id,
                            "reason_code": "renewal_advanced_after_expiration_apply",
                        },
                    )
                    await lease_repository.release(
                        user_id=subscription.user_id, owner_key=owner_key
                    )
                    await self._session.commit()
                    continue

                await subscription_repository.mark_expired(subscription)
                await EntitlementRepository(self._session).set_authoritative(
                    subscription,
                    version=applied.version,
                    status=applied.status,
                    valid_until_utc=applied.valid_until_utc,
                    max_device_tokens=applied.max_device_tokens,
                )
                await TrialClaimRepository(self._session).mark_terminal_for_subscription(
                    subscription.id,
                    terminal_status=TRIAL_STATUS_EXPIRED,
                    occurred_at_utc=utc_now(),
                )
                await EntitlementOperationRepository(self._session).mark_completed(operation)
                await NotificationOutboxRepository(self._session).enqueue_once(
                    idempotency_key=f"subscription-expired:{operation.public_id}",
                    notification_kind="subscription_expired",
                    subscription_id=subscription.id,
                    user_id=subscription.user_id,
                    payload={"operation_public_id": operation.public_id},
                )
                await lease_repository.release(user_id=subscription.user_id, owner_key=owner_key)
                await self._session.commit()
                expired_count += 1
            except IntegrityError:
                await self._session.rollback()
                continue
            except MediatorClientError:
                await self._session.rollback()
                await AccessOperationLeaseRepository(self._session).release(
                    user_id=subscription.user_id, owner_key=owner_key
                )
                await self._session.commit()
                continue

        return expired_count

    async def disable_expired_subscriptions(self) -> int:
        return await self.expire_due_subscriptions()


async def run_expiration_worker(
    session_factory,
    mediator_client: MediatorClient,
    interval_seconds: int,
    failure_limit: int = 5,
) -> None:
    backoff_seconds = min(max(interval_seconds, 1), 60)
    consecutive_failures = 0

    while True:
        try:
            async with session_factory() as session:
                service = ExpirationService(session, mediator_client)
                expired_count = await service.expire_due_subscriptions()

            logger.info("Expiration worker completed: expired_count=%s", expired_count)
            backoff_seconds = min(max(interval_seconds, 1), 60)
            consecutive_failures = 0
            await asyncio.sleep(interval_seconds)
        except asyncio.CancelledError:
            raise
        except Exception as exception:
            consecutive_failures += 1
            logger.exception("Expiration worker iteration failed.")
            if consecutive_failures >= max(failure_limit, 1):
                raise RuntimeError(
                    "Critical expiration worker exceeded its failure limit."
                ) from exception
            await asyncio.sleep(backoff_seconds)
            backoff_seconds = min(backoff_seconds * 2, 300)
