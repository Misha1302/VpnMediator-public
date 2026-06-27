from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from enum import StrEnum

from vpn_access_bot.config import Settings
from vpn_access_bot.constants import (
    ORDER_KIND_EXTEND,
    ORDER_KIND_EXTEND_AND_UPGRADE,
    ORDER_KIND_PURCHASE,
    ORDER_KIND_RESUME,
    ORDER_KIND_UPGRADE_DEVICES,
    ORDER_STATUS_ACTIVATING,
    ORDER_STATUS_ACTIVATION_FAILED,
    ORDER_STATUS_PAYMENT_RECEIVED,
    ORDER_STATUS_PENDING,
    PAYMENT_MODE_TELEGRAM_STARS,
    SUBSCRIPTION_STATUS_DISABLED,
    SUBSCRIPTION_STATUS_EXPIRED,
)
from vpn_access_bot.models import Order, OrderApplication, PurchaseQuote, Subscription
from vpn_access_bot.product_catalog import ProductCatalog
from vpn_access_bot.repositories import to_aware_utc
from vpn_access_bot.trial import TrialEligibility

PRODUCT_MONTH_DAYS = 30


class UserErrorCode(StrEnum):
    NO_SUBSCRIPTION = "NO_SUBSCRIPTION"
    SUBSCRIPTION_EXPIRED = "SUBSCRIPTION_EXPIRED"
    DEVICE_LIMIT_REACHED = "DEVICE_LIMIT_REACHED"
    RESET_COOLDOWN = "RESET_COOLDOWN"
    PAYMENT_FAILED = "PAYMENT_FAILED"
    ACTIVATION_FAILED = "ACTIVATION_FAILED"
    SERVICE_UNAVAILABLE = "SERVICE_UNAVAILABLE"
    ORDER_EXPIRED = "ORDER_EXPIRED"
    ORDER_ALREADY_PAID = "ORDER_ALREADY_PAID"
    QUOTE_EXPIRED = "QUOTE_EXPIRED"


class SubscriptionLifecycleState(StrEnum):
    NONE = "none"
    ACTIVE = "active"
    EXPIRED = "expired"
    DISABLED = "disabled"


CabinetSubscriptionState = SubscriptionLifecycleState


class OrderNoticeState(StrEnum):
    NONE = "none"
    PAYMENT_PENDING = "payment_pending"
    PAYMENT_RECEIVED = "payment_received"
    ACTIVATING = "activating"
    ACTIVATION_FAILED = "activation_failed"


@dataclass(frozen=True)
class CalculatedPrice:
    period_count: int
    duration_days: int
    max_devices: int
    amount_minor_units: int
    currency: str
    pricing_version: str
    upgrade_amount_minor_units: int = 0
    extension_amount_minor_units: int = 0
    price_before_personal_discount: int = 0
    personal_discount_bps: int = 0
    personal_discount_amount_minor_units: int = 0


@dataclass(frozen=True)
class CabinetState:
    subscription_state: SubscriptionLifecycleState
    order_notice_state: OrderNoticeState
    valid_until_utc: datetime | None
    remaining_days: int | None
    active_device_tokens: int | None
    max_device_tokens: int | None
    mediator_available: bool
    commerce_available: bool
    trial_available: bool
    trial_retry_available: bool
    pending_order_public_id: str | None
    pending_order_kind: str | None

    @property
    def latest_order_state(self) -> str | None:
        if self.order_notice_state == OrderNoticeState.NONE:
            return None
        return self.order_notice_state.value

    @property
    def primary_action(self) -> str:
        if self.order_notice_state == OrderNoticeState.ACTIVATION_FAILED:
            return "recover_access"
        if self.order_notice_state != OrderNoticeState.NONE:
            return "continue_order"
        if self.subscription_state == SubscriptionLifecycleState.ACTIVE:
            return "add_device"
        if self.subscription_state == SubscriptionLifecycleState.EXPIRED:
            return "resume"
        if self.subscription_state == SubscriptionLifecycleState.DISABLED:
            return "support"
        return "purchase"

    @property
    def secondary_actions(self) -> tuple[str, ...]:
        if self.order_notice_state == OrderNoticeState.PAYMENT_PENDING:
            return ("cancel_order", "payment_help")
        if self.order_notice_state in {
            OrderNoticeState.PAYMENT_RECEIVED,
            OrderNoticeState.ACTIVATING,
            OrderNoticeState.ACTIVATION_FAILED,
        }:
            return ("payment_help",)
        if self.subscription_state == SubscriptionLifecycleState.ACTIVE:
            return ("devices", "renew", "more")
        if self.subscription_state == SubscriptionLifecycleState.EXPIRED:
            return ("devices", "support")
        return ("how_it_works", "support")


class PricingService:
    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._catalog = ProductCatalog.from_settings(settings)

    @property
    def catalog(self) -> ProductCatalog:
        return self._catalog

    def calculate(self, period_count: int, max_devices: int) -> CalculatedPrice:
        self._catalog.validate_period(period_count)
        self._catalog.validate_device_limit(max_devices)
        return self.calculate_operation(
            operation_kind=ORDER_KIND_PURCHASE,
            period_count=period_count,
            requested_max_devices=max_devices,
        )

    def calculate_operation(
        self,
        operation_kind: str,
        period_count: int,
        requested_max_devices: int,
        current_max_devices: int = 0,
        remaining_paid_seconds: int = 0,
        personal_discount_bps: int = 0,
        payment_provider: str = PAYMENT_MODE_TELEGRAM_STARS,
    ) -> CalculatedPrice:
        supported_operations = {
            ORDER_KIND_PURCHASE,
            ORDER_KIND_EXTEND,
            ORDER_KIND_EXTEND_AND_UPGRADE,
            ORDER_KIND_RESUME,
            ORDER_KIND_UPGRADE_DEVICES,
        }
        if operation_kind not in supported_operations:
            raise ValueError("unsupported_order_kind")
        if not 0 <= personal_discount_bps <= 10_000:
            raise ValueError("invalid_personal_discount")
        if remaining_paid_seconds < 0:
            raise ValueError("invalid_remaining_paid_time")

        is_upgrade_only = operation_kind == ORDER_KIND_UPGRADE_DEVICES
        self._catalog.validate_period(period_count, allow_zero=is_upgrade_only)
        if is_upgrade_only and period_count != 0:
            raise ValueError("upgrade_period_must_be_zero")
        if not is_upgrade_only and period_count == 0:
            raise ValueError("paid_period_required")

        grandfathered = (
            current_max_devices
            if operation_kind
            in {
                ORDER_KIND_EXTEND,
                ORDER_KIND_RESUME,
            }
            else None
        )
        self._catalog.validate_device_limit(
            requested_max_devices,
            grandfathered_value=grandfathered,
        )
        if requested_max_devices < current_max_devices:
            raise ValueError("device_limit_decrease_not_allowed")
        if is_upgrade_only and requested_max_devices <= current_max_devices:
            raise ValueError("device_limit_must_increase")

        paid_duration_days = period_count * PRODUCT_MONTH_DAYS
        monthly_device_price = self._catalog.base_price_for_provider(payment_provider)
        upgrade_amount = 0
        extension_amount = 0

        if operation_kind in {ORDER_KIND_UPGRADE_DEVICES, ORDER_KIND_EXTEND_AND_UPGRADE}:
            added_devices = requested_max_devices - current_max_devices
            if added_devices > 0 and remaining_paid_seconds > 0:
                upgrade_amount = _ceil_div(
                    added_devices * monthly_device_price * remaining_paid_seconds,
                    PRODUCT_MONTH_DAYS * 24 * 60 * 60,
                )

        if operation_kind in {
            ORDER_KIND_PURCHASE,
            ORDER_KIND_EXTEND,
            ORDER_KIND_EXTEND_AND_UPGRADE,
            ORDER_KIND_RESUME,
        }:
            gross_extension = period_count * requested_max_devices * monthly_device_price
            duration_discount_percent = self._catalog.duration_discounts.get(period_count, 0)
            duration_discount = gross_extension * duration_discount_percent // 100
            extension_amount = gross_extension - duration_discount

        subtotal = upgrade_amount + extension_amount
        personal_discount_amount = subtotal * personal_discount_bps // 10_000
        final_amount = subtotal - personal_discount_amount
        is_complimentary = subtotal > 0 and personal_discount_bps == 10_000 and final_amount == 0
        if final_amount <= 0 and not is_complimentary:
            raise ValueError("non_positive_order_price")

        return CalculatedPrice(
            period_count=period_count,
            duration_days=paid_duration_days,
            max_devices=requested_max_devices,
            amount_minor_units=final_amount,
            currency=self._catalog.currency_for_provider(payment_provider),
            pricing_version=self._catalog.pricing_identity_for_provider(payment_provider),
            upgrade_amount_minor_units=upgrade_amount,
            extension_amount_minor_units=extension_amount,
            price_before_personal_discount=subtotal,
            personal_discount_bps=personal_discount_bps,
            personal_discount_amount_minor_units=personal_discount_amount,
        )

    def calculate_quote_offer(self, quote: PurchaseQuote, payment_provider: str) -> CalculatedPrice:
        return self.calculate_operation(
            operation_kind=quote.order_kind,
            period_count=quote.period_count,
            requested_max_devices=quote.requested_max_devices or quote.max_devices,
            current_max_devices=quote.base_max_devices or 0,
            remaining_paid_seconds=quote.remaining_paid_seconds_at_quote,
            personal_discount_bps=quote.personal_discount_bps,
            payment_provider=payment_provider,
        )


class CabinetStateBuilder:
    def build(
        self,
        subscription: Subscription | None,
        latest_order: Order | None,
        trial_eligibility: TrialEligibility,
        active_device_tokens: int | None = 0,
        mediator_available: bool = True,
        commerce_available: bool = True,
        now: datetime | None = None,
    ) -> CabinetState:
        now = to_aware_utc(now or datetime.now(UTC))
        order_notice = self._order_notice(latest_order)

        if subscription is None:
            subscription_state = SubscriptionLifecycleState.NONE
            expires_at = None
            remaining_days = None
            max_devices = 0
        else:
            expires_at = to_aware_utc(subscription.expires_at)
            remaining_days = max((expires_at.date() - now.date()).days, 0)
            max_devices = subscription.max_devices
            if subscription.status == SUBSCRIPTION_STATUS_DISABLED:
                subscription_state = SubscriptionLifecycleState.DISABLED
            elif subscription.status == SUBSCRIPTION_STATUS_EXPIRED or expires_at <= now:
                subscription_state = SubscriptionLifecycleState.EXPIRED
            else:
                subscription_state = SubscriptionLifecycleState.ACTIVE

        return CabinetState(
            subscription_state=subscription_state,
            order_notice_state=order_notice,
            valid_until_utc=expires_at,
            remaining_days=remaining_days,
            active_device_tokens=active_device_tokens,
            max_device_tokens=max_devices,
            mediator_available=mediator_available,
            commerce_available=commerce_available,
            trial_available=trial_eligibility.is_available,
            trial_retry_available=trial_eligibility.can_retry_failed_activation,
            pending_order_public_id=(
                latest_order.public_order_id if latest_order is not None else None
            ),
            pending_order_kind=(latest_order.order_kind if latest_order is not None else None),
        )

    @staticmethod
    def _order_notice(order: Order | None) -> OrderNoticeState:
        if order is None:
            return OrderNoticeState.NONE
        return {
            ORDER_STATUS_PENDING: OrderNoticeState.PAYMENT_PENDING,
            ORDER_STATUS_PAYMENT_RECEIVED: OrderNoticeState.PAYMENT_RECEIVED,
            ORDER_STATUS_ACTIVATING: OrderNoticeState.ACTIVATING,
            ORDER_STATUS_ACTIVATION_FAILED: OrderNoticeState.ACTIVATION_FAILED,
        }.get(order.status, OrderNoticeState.NONE)


class RefundPolicy:
    def automatic_refund_allowed(
        self,
        order: Order,
        application: OrderApplication | None,
    ) -> tuple[bool, str]:
        if application is not None:
            return False, "Доступ уже был выдан. Автоматический возврат невозможен."

        if order.status not in {ORDER_STATUS_PAYMENT_RECEIVED, ORDER_STATUS_ACTIVATION_FAILED}:
            return False, "Возврат доступен только если оплата получена, а доступ не был выдан."

        return True, "Заказ можно вернуть автоматически."


def calculate_renewal_valid_until(
    current_valid_until: datetime | None,
    duration_days: int,
    now: datetime | None = None,
) -> datetime:
    now = to_aware_utc(now or datetime.now(UTC))
    base = now

    if current_valid_until is not None:
        existing = to_aware_utc(current_valid_until)

        if existing > now:
            base = existing

    return base + timedelta(days=duration_days)


def _ceil_div(numerator: int, denominator: int) -> int:
    if denominator <= 0:
        raise ValueError("denominator must be positive")

    return (numerator + denominator - 1) // denominator


ERROR_MESSAGES: dict[UserErrorCode, str] = {
    UserErrorCode.NO_SUBSCRIPTION: "Доступа пока нет. Нажмите «Купить доступ».",
    UserErrorCode.SUBSCRIPTION_EXPIRED: "Доступ закончился. Нажмите «Возобновить доступ».",
    UserErrorCode.DEVICE_LIMIT_REACHED: (
        "Достигнут лимит устройств. Отключите ненужное устройство или увеличьте лимит."
    ),
    UserErrorCode.RESET_COOLDOWN: (
        "Сброс пока недоступен. Попробуйте позже или напишите в поддержку."
    ),
    UserErrorCode.PAYMENT_FAILED: (
        "Оплата не завершилась. Повторите оплату или напишите в поддержку."
    ),
    UserErrorCode.ACTIVATION_FAILED: (
        "Оплата получена, но изменение доступа пока не применилось. Повторите активацию."
    ),
    UserErrorCode.SERVICE_UNAVAILABLE: (
        "Сервис временно недоступен. Попробуйте позже или напишите в поддержку."
    ),
    UserErrorCode.ORDER_EXPIRED: "Заказ устарел. Создайте новый заказ.",
    UserErrorCode.ORDER_ALREADY_PAID: "Этот заказ уже оплачен. Откройте «Мой доступ».",
    UserErrorCode.QUOTE_EXPIRED: "Расчёт цены устарел. Проверьте заказ ещё раз.",
}
