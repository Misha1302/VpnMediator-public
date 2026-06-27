from __future__ import annotations

from datetime import UTC, datetime
from uuid import uuid4

from sqlalchemy import (
    BigInteger,
    Boolean,
    DateTime,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


def utc_now() -> datetime:
    return datetime.now(UTC)


class Base(DeclarativeBase):
    pass


class User(Base):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    telegram_id: Mapped[int] = mapped_column(BigInteger, unique=True, index=True, nullable=False)
    username: Mapped[str | None] = mapped_column(String(128), nullable=True)
    first_name: Mapped[str | None] = mapped_column(String(128), nullable=True)
    primary_subscription_id: Mapped[int | None] = mapped_column(Integer, nullable=True, index=True)
    platform_preference: Mapped[str | None] = mapped_column(String(32), nullable=True)
    referral_code: Mapped[str | None] = mapped_column(String(32), unique=True, nullable=True)
    referred_by_user_id: Mapped[int | None] = mapped_column(
        ForeignKey("users.id"), nullable=True, index=True
    )
    referred_at_utc: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    referral_blocked: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    test_user_reset_generation: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    test_user_reset_at_utc: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, nullable=False
    )

    orders: Mapped[list[Order]] = relationship(back_populates="user")
    subscriptions: Mapped[list[Subscription]] = relationship(back_populates="user")
    purchase_quotes: Mapped[list[PurchaseQuote]] = relationship(back_populates="user")


class TestUserResetOperation(Base):
    __tablename__ = "test_user_reset_operations"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    source_request_id: Mapped[str] = mapped_column(
        String(160), unique=True, nullable=False, index=True
    )
    target_telegram_id: Mapped[int] = mapped_column(BigInteger, nullable=False, index=True)
    actor_telegram_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    state: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    outcome_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at_utc: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    completed_at_utc: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )


class TelegramBotChannel(Base):
    __tablename__ = "telegram_bot_channels"

    bot_key: Mapped[str] = mapped_column(String(32), primary_key=True)
    telegram_bot_id: Mapped[int] = mapped_column(BigInteger, unique=True, nullable=False)
    username: Mapped[str] = mapped_column(String(128), nullable=False)
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    required: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    last_verified_at_utc: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    last_update_at_utc: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )


class UserBotChannel(Base):
    __tablename__ = "user_bot_channels"

    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), primary_key=True)
    bot_key: Mapped[str] = mapped_column(
        ForeignKey("telegram_bot_channels.bot_key"), primary_key=True
    )
    first_seen_at_utc: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    last_seen_at_utc: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    can_receive_messages: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    blocked_at_utc: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class Tariff(Base):
    __tablename__ = "tariffs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    code: Mapped[str] = mapped_column(String(64), unique=True, index=True, nullable=False)
    title: Mapped[str] = mapped_column(String(128), nullable=False)
    description: Mapped[str] = mapped_column(Text, nullable=False)
    price_minor_units: Mapped[int] = mapped_column(Integer, nullable=False)
    currency: Mapped[str] = mapped_column(String(8), nullable=False)
    duration_days: Mapped[int] = mapped_column(Integer, nullable=False)
    max_devices: Mapped[int] = mapped_column(Integer, nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    sort_order: Mapped[int] = mapped_column(Integer, default=0, nullable=False)

    orders: Mapped[list[Order]] = relationship(back_populates="tariff")
    subscriptions: Mapped[list[Subscription]] = relationship(back_populates="tariff")


class PurchaseQuote(Base):
    __tablename__ = "purchase_quotes"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    public_quote_id: Mapped[str] = mapped_column(
        String(36), unique=True, index=True, default=lambda: str(uuid4()), nullable=False
    )
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), nullable=False, index=True)
    origin_bot_key: Mapped[str | None] = mapped_column(String(32), nullable=True, index=True)
    period_count: Mapped[int] = mapped_column(Integer, nullable=False)
    duration_days: Mapped[int] = mapped_column(Integer, nullable=False)
    max_devices: Mapped[int] = mapped_column(Integer, nullable=False)
    amount_minor_units: Mapped[int] = mapped_column(Integer, nullable=False)
    currency: Mapped[str] = mapped_column(String(8), nullable=False)
    pricing_version: Mapped[str] = mapped_column(String(64), nullable=False)
    target_subscription_id: Mapped[int | None] = mapped_column(
        ForeignKey("subscriptions.id"), nullable=True, index=True
    )
    order_kind: Mapped[str] = mapped_column(String(32), nullable=False)
    base_entitlement_version: Mapped[int | None] = mapped_column(Integer, nullable=True)
    base_valid_until_utc: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    base_max_devices: Mapped[int | None] = mapped_column(Integer, nullable=True)
    requested_max_devices: Mapped[int | None] = mapped_column(Integer, nullable=True)
    requested_duration_days: Mapped[int | None] = mapped_column(Integer, nullable=True)
    upgrade_amount_minor_units: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    extension_amount_minor_units: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    price_before_personal_discount: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    personal_discount_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    personal_discount_bps: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    personal_discount_amount_minor_units: Mapped[int] = mapped_column(
        Integer, default=0, nullable=False
    )
    final_amount_minor_units: Mapped[int | None] = mapped_column(Integer, nullable=True)
    referral_eligible: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    is_test_order: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    trial_claim_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    trial_seconds_remaining_at_quote: Mapped[int] = mapped_column(
        Integer, default=0, nullable=False
    )
    remaining_paid_seconds_at_quote: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    expires_at_utc: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    created_at_utc: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, nullable=False
    )
    consumed_at_utc: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    user: Mapped[User] = relationship(back_populates="purchase_quotes")
    target_subscription: Mapped[Subscription | None] = relationship()


class Order(Base):
    __tablename__ = "orders"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    public_order_id: Mapped[str] = mapped_column(
        String(36), unique=True, index=True, default=lambda: str(uuid4()), nullable=False
    )
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), nullable=False, index=True)
    origin_bot_key: Mapped[str | None] = mapped_column(String(32), nullable=True, index=True)
    payment_bot_key: Mapped[str | None] = mapped_column(String(32), nullable=True, index=True)
    tariff_id: Mapped[int | None] = mapped_column(ForeignKey("tariffs.id"), nullable=True)
    quote_id: Mapped[int | None] = mapped_column(
        ForeignKey("purchase_quotes.id"), nullable=True, index=True
    )
    period_count: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    duration_days: Mapped[int] = mapped_column(Integer, default=30, nullable=False)
    selected_max_devices: Mapped[int] = mapped_column(Integer, default=3, nullable=False)
    pricing_version: Mapped[str] = mapped_column(
        String(64), default="legacy-tariff", nullable=False
    )
    target_subscription_id: Mapped[int | None] = mapped_column(
        ForeignKey("subscriptions.id"), nullable=True, index=True
    )
    order_kind: Mapped[str] = mapped_column(String(32), default="purchase", nullable=False)
    base_entitlement_version: Mapped[int | None] = mapped_column(Integer, nullable=True)
    base_valid_until_utc: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    base_max_devices: Mapped[int | None] = mapped_column(Integer, nullable=True)
    requested_max_devices: Mapped[int | None] = mapped_column(Integer, nullable=True)
    requested_duration_days: Mapped[int | None] = mapped_column(Integer, nullable=True)
    upgrade_amount_minor_units: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    extension_amount_minor_units: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    price_before_personal_discount: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    personal_discount_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    personal_discount_bps: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    personal_discount_amount_minor_units: Mapped[int] = mapped_column(
        Integer, default=0, nullable=False
    )
    final_amount_minor_units: Mapped[int | None] = mapped_column(Integer, nullable=True)
    referral_eligible: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    is_test_order: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    trial_claim_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    trial_seconds_remaining_at_quote: Mapped[int] = mapped_column(
        Integer, default=0, nullable=False
    )
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="pending", index=True)
    amount_minor_units: Mapped[int] = mapped_column(Integer, nullable=False)
    currency: Mapped[str] = mapped_column(String(8), nullable=False)
    provider: Mapped[str] = mapped_column(String(32), nullable=False)
    provider_payment_id: Mapped[str | None] = mapped_column(String(256), nullable=True)
    provider_payment_status: Mapped[str | None] = mapped_column(String(32), nullable=True)
    provider_confirmation_url: Mapped[str | None] = mapped_column(Text, nullable=True)
    invoice_payload: Mapped[str] = mapped_column(String(128), unique=True, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, nullable=False
    )
    expires_at_utc: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    checkout_authorized_at_utc: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    checkout_authorized_until_utc: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    paid_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    cancelled_at_utc: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    completed_at_utc: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    activation_attempt_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    last_activation_attempt_at_utc: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    next_activation_retry_at_utc: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    last_activation_error_code: Mapped[str | None] = mapped_column(String(64), nullable=True)
    base_expires_at_utc: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    purchased_duration_days: Mapped[int | None] = mapped_column(Integer, nullable=True)
    expiration_policy_version: Mapped[str | None] = mapped_column(String(64), nullable=True)
    target_expires_at_utc: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    user: Mapped[User] = relationship(back_populates="orders")
    tariff: Mapped[Tariff | None] = relationship(back_populates="orders")
    quote: Mapped[PurchaseQuote | None] = relationship()
    application: Mapped[OrderApplication | None] = relationship(back_populates="order")

    __table_args__ = (
        UniqueConstraint("provider", "provider_payment_id", name="uq_orders_provider_payment_id"),
    )


class PaymentInbox(Base):
    __tablename__ = "payment_inbox"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    provider: Mapped[str] = mapped_column(String(32), nullable=False)
    provider_charge_id: Mapped[str] = mapped_column(String(256), nullable=False)
    invoice_payload_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    invoice_payload: Mapped[str | None] = mapped_column(String(128), nullable=True)
    payer_external_id: Mapped[str] = mapped_column(String(128), nullable=False)
    amount_minor_units: Mapped[int] = mapped_column(Integer, nullable=False)
    currency: Mapped[str] = mapped_column(String(8), nullable=False)
    received_at_utc: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    provider_occurred_at_utc: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    origin_bot_key: Mapped[str | None] = mapped_column(String(32), nullable=True, index=True)
    payment_bot_key: Mapped[str | None] = mapped_column(String(32), nullable=True, index=True)
    matched_order_id: Mapped[int | None] = mapped_column(
        ForeignKey("orders.id"), nullable=True, index=True
    )
    reconciliation_status: Mapped[str] = mapped_column(
        String(32), nullable=False, default="received", index=True
    )
    failure_code: Mapped[str | None] = mapped_column(String(64), nullable=True)
    attempt_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    last_attempt_at_utc: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    next_attempt_at_utc: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    claimed_by: Mapped[str | None] = mapped_column(String(64), nullable=True)
    claim_expires_at_utc: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    processed_at_utc: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    updated_at_utc: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    __table_args__ = (
        UniqueConstraint(
            "provider",
            "provider_charge_id",
            name="uq_payment_inbox_provider_charge",
        ),
    )


class TelegramUpdateInbox(Base):
    __tablename__ = "telegram_update_inbox"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    bot_key: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    update_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    payload_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    payload_json: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="pending", index=True)
    attempt_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    received_at_utc: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    last_attempt_at_utc: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    next_attempt_at_utc: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    claimed_by: Mapped[str | None] = mapped_column(String(64), nullable=True)
    claim_expires_at_utc: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    processed_at_utc: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    failure_code: Mapped[str | None] = mapped_column(String(64), nullable=True)
    last_error_message: Mapped[str | None] = mapped_column(String(512), nullable=True)
    updated_at_utc: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    __table_args__ = (
        UniqueConstraint("bot_key", "update_id", name="uq_telegram_update_inbox_bot_update"),
    )


class EntitlementOperation(Base):
    __tablename__ = "entitlement_operations"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    public_id: Mapped[str] = mapped_column(String(36), nullable=False, unique=True, index=True)
    subscription_id: Mapped[int | None] = mapped_column(
        ForeignKey("subscriptions.id"), nullable=True, index=True
    )
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), nullable=False, index=True)
    operation_type: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    source_entity_type: Mapped[str] = mapped_column(String(32), nullable=False)
    source_entity_id: Mapped[str] = mapped_column(String(128), nullable=False)
    idempotency_key: Mapped[str] = mapped_column(String(160), nullable=False, unique=True)
    expected_version: Mapped[int | None] = mapped_column(Integer, nullable=True)
    duration_delta_seconds: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    requested_device_limit: Mapped[int | None] = mapped_column(Integer, nullable=True)
    requested_status: Mapped[str | None] = mapped_column(String(32), nullable=True)
    observed_valid_until_utc: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    intended_valid_until_utc: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    state: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    external_result_version: Mapped[int | None] = mapped_column(Integer, nullable=True)
    external_result_status: Mapped[str | None] = mapped_column(String(32), nullable=True)
    external_result_valid_until_utc: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    external_result_device_limit: Mapped[int | None] = mapped_column(Integer, nullable=True)
    external_subscription_public_guid: Mapped[str | None] = mapped_column(String(36), nullable=True)
    external_request_sent_at_utc: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    external_applied_at_utc: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    local_commit_completed_at_utc: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    claimed_by: Mapped[str | None] = mapped_column(String(64), nullable=True)
    claim_expires_at_utc: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    attempt_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    last_error_code: Mapped[str | None] = mapped_column(String(64), nullable=True)
    last_error_at_utc: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    created_at_utc: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    updated_at_utc: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    __table_args__ = (
        UniqueConstraint(
            "source_entity_type",
            "source_entity_id",
            "operation_type",
            name="uq_entitlement_operation_source",
        ),
    )


class RefundOperation(Base):
    __tablename__ = "refund_operations"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    public_id: Mapped[str] = mapped_column(String(36), nullable=False, unique=True, index=True)
    order_id: Mapped[int] = mapped_column(ForeignKey("orders.id"), nullable=False, unique=True)
    subscription_id: Mapped[int | None] = mapped_column(
        ForeignKey("subscriptions.id"), nullable=True, index=True
    )
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), nullable=False, index=True)
    idempotency_key: Mapped[str] = mapped_column(String(160), nullable=False, unique=True)
    state: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    provider: Mapped[str] = mapped_column(String(32), nullable=False)
    provider_charge_reference_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    provider_requested_at_utc: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    provider_refunded_at_utc: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    compensation_operation_id: Mapped[int | None] = mapped_column(
        ForeignKey("entitlement_operations.id"), nullable=True
    )
    attempt_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    last_error_code: Mapped[str | None] = mapped_column(String(64), nullable=True)
    created_at_utc: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    updated_at_utc: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    completed_at_utc: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )


class RefundPlan(Base):
    __tablename__ = "refund_plans"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    public_id: Mapped[str] = mapped_column(String(36), nullable=False, unique=True, index=True)
    refund_operation_id: Mapped[int] = mapped_column(
        ForeignKey("refund_operations.id"), nullable=False, unique=True
    )
    order_id: Mapped[int] = mapped_column(ForeignKey("orders.id"), nullable=False, unique=True)
    subscription_id: Mapped[int | None] = mapped_column(
        ForeignKey("subscriptions.id"), nullable=True, index=True
    )
    expected_current_entitlement_version: Mapped[int | None] = mapped_column(Integer, nullable=True)
    previous_status: Mapped[str | None] = mapped_column(String(32), nullable=True)
    previous_valid_until_utc: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    previous_max_devices: Mapped[int | None] = mapped_column(Integer, nullable=True)
    target_status: Mapped[str] = mapped_column(String(32), nullable=False)
    target_valid_until_utc: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    target_max_devices: Mapped[int | None] = mapped_column(Integer, nullable=True)
    source_order_kind: Mapped[str] = mapped_column(String(32), nullable=False)
    computation_version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    evidence_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    state: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    confirmation_token_hash: Mapped[str | None] = mapped_column(
        String(64), nullable=True, unique=True, index=True
    )
    confirmation_expires_at_utc: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    created_by_admin_telegram_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    confirmed_at_utc: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    created_at_utc: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    updated_at_utc: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    failure_code: Mapped[str | None] = mapped_column(String(64), nullable=True)


class CommercePolicy(Base):
    __tablename__ = "commerce_policy"

    singleton_id: Mapped[int] = mapped_column(Integer, primary_key=True, default=1)
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    new_purchases_enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    trials_enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    renewals_enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    resumes_enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    device_upgrades_enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    extend_and_upgrade_enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    referrals_enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    campaign_tracking_enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    capacity_enforcement_enabled: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False
    )
    reason_code: Mapped[str] = mapped_column(
        String(64), nullable=False, default="pre_advertising_freeze"
    )
    operator_note: Mapped[str | None] = mapped_column(Text, nullable=True)
    updated_by_admin_telegram_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    updated_at_utc: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    expires_at_utc: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class CommercePolicyChangeRequest(Base):
    __tablename__ = "commerce_policy_change_requests"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    public_id: Mapped[str] = mapped_column(String(36), nullable=False, unique=True, index=True)
    confirmation_token_hash: Mapped[str] = mapped_column(
        String(64), nullable=False, unique=True, index=True
    )
    admin_telegram_id: Mapped[int] = mapped_column(BigInteger, nullable=False, index=True)
    switch_name: Mapped[str] = mapped_column(String(32), nullable=False)
    requested_enabled: Mapped[bool] = mapped_column(Boolean, nullable=False)
    expected_policy_version: Mapped[int] = mapped_column(Integer, nullable=False)
    reason_code: Mapped[str] = mapped_column(String(64), nullable=False)
    operator_note: Mapped[str | None] = mapped_column(Text, nullable=True)
    state: Mapped[str] = mapped_column(String(24), nullable=False, index=True)
    expires_at_utc: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    created_at_utc: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    confirmed_at_utc: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    failure_code: Mapped[str | None] = mapped_column(String(64), nullable=True)


class NotificationOutbox(Base):
    __tablename__ = "notification_outbox"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    public_id: Mapped[str] = mapped_column(String(36), nullable=False, unique=True, index=True)
    idempotency_key: Mapped[str] = mapped_column(String(160), nullable=False, unique=True)
    user_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"), nullable=True, index=True)
    subscription_id: Mapped[int | None] = mapped_column(
        ForeignKey("subscriptions.id"), nullable=True, index=True
    )
    order_id: Mapped[int | None] = mapped_column(ForeignKey("orders.id"), nullable=True)
    broadcast_campaign_id: Mapped[int | None] = mapped_column(
        ForeignKey("broadcast_campaigns.id"), nullable=True, index=True
    )
    bot_key: Mapped[str | None] = mapped_column(String(32), nullable=True)
    delivery_bot_key: Mapped[str | None] = mapped_column(String(32), nullable=True, index=True)
    notification_kind: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    payload_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    state: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    attempt_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    available_at_utc: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    claimed_at_utc: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    provider_accepted_at_utc: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    failed_at_utc: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_error_code: Mapped[str | None] = mapped_column(String(64), nullable=True)
    created_at_utc: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    updated_at_utc: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class BroadcastCampaign(Base):
    __tablename__ = "broadcast_campaigns"
    __table_args__ = (
        UniqueConstraint(
            "source_bot_key",
            "source_chat_id",
            "source_message_id",
            name="ux_broadcast_campaign_source_message",
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    public_id: Mapped[str] = mapped_column(String(36), nullable=False, unique=True, index=True)
    admin_telegram_id: Mapped[int] = mapped_column(BigInteger, nullable=False, index=True)
    source_bot_key: Mapped[str] = mapped_column(String(32), nullable=False)
    source_chat_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    source_message_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    filter_kind: Mapped[str] = mapped_column(String(16), nullable=False)
    recipient_pattern: Mapped[str | None] = mapped_column(String(128), nullable=True)
    recipient_upper_bound_user_id: Mapped[int] = mapped_column(Integer, nullable=False)
    message_text: Mapped[str] = mapped_column(Text, nullable=False)
    message_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    confirmation_token_hash: Mapped[str] = mapped_column(
        String(64), nullable=False, unique=True, index=True
    )
    state: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    target_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    queued_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    expires_at_utc: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    confirmed_at_utc: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    queued_at_utc: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at_utc: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    updated_at_utc: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class BroadcastRecipient(Base):
    __tablename__ = "broadcast_recipients"

    campaign_id: Mapped[int] = mapped_column(ForeignKey("broadcast_campaigns.id"), primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), primary_key=True)
    telegram_id: Mapped[int] = mapped_column(BigInteger, nullable=False, index=True)
    created_at_utc: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class Subscription(Base):
    __tablename__ = "subscriptions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), nullable=False, index=True)
    tariff_id: Mapped[int | None] = mapped_column(ForeignKey("tariffs.id"), nullable=True)
    public_guid: Mapped[str] = mapped_column(String(36), unique=True, index=True, nullable=False)
    signed_url: Mapped[str] = mapped_column(Text, nullable=False)
    max_devices: Mapped[int] = mapped_column(Integer, nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="active", index=True)
    starts_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    expires_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, index=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, nullable=False
    )
    updated_at_utc: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, nullable=False
    )
    disabled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    reconciliation_state: Mapped[str] = mapped_column(
        String(32), nullable=False, default="healthy", index=True
    )
    reconciliation_reason: Mapped[str | None] = mapped_column(String(64), nullable=True)
    reconciliation_blocked_at_utc: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    test_reset_at_utc: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True, index=True
    )

    user: Mapped[User] = relationship(back_populates="subscriptions")
    tariff: Mapped[Tariff | None] = relationship(back_populates="subscriptions")
    reset_events: Mapped[list[DeviceResetEvent]] = relationship(back_populates="subscription")
    entitlement: Mapped[AccessEntitlement | None] = relationship(back_populates="subscription")


class DeviceResetEvent(Base):
    __tablename__ = "device_reset_events"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    subscription_id: Mapped[int] = mapped_column(
        ForeignKey("subscriptions.id"), nullable=False, index=True
    )
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), nullable=False, index=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, nullable=False
    )

    subscription: Mapped[Subscription] = relationship(back_populates="reset_events")

    __table_args__ = (
        UniqueConstraint(
            "subscription_id", "created_at", name="uq_device_reset_subscription_created_at"
        ),
    )


class AccessEntitlement(Base):
    __tablename__ = "access_entitlements"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    subscription_id: Mapped[int] = mapped_column(
        ForeignKey("subscriptions.id"), nullable=False, unique=True, index=True
    )
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    valid_until_utc: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    max_device_tokens: Mapped[int] = mapped_column(Integer, nullable=False)
    updated_at_utc: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, nullable=False
    )

    subscription: Mapped[Subscription] = relationship(back_populates="entitlement")


class OrderApplication(Base):
    __tablename__ = "order_applications"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    order_id: Mapped[int] = mapped_column(
        ForeignKey("orders.id"), nullable=False, unique=True, index=True
    )
    subscription_id: Mapped[int] = mapped_column(
        ForeignKey("subscriptions.id"), nullable=False, index=True
    )
    applied_at_utc: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    duration_days: Mapped[int] = mapped_column(Integer, nullable=False)
    selected_max_devices: Mapped[int] = mapped_column(Integer, nullable=False)
    resulting_valid_until_utc: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    resulting_entitlement_version: Mapped[int] = mapped_column(Integer, nullable=False)
    previous_entitlement_version: Mapped[int | None] = mapped_column(Integer, nullable=True)
    previous_status: Mapped[str | None] = mapped_column(String(32), nullable=True)
    previous_valid_until_utc: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    previous_max_devices: Mapped[int | None] = mapped_column(Integer, nullable=True)

    order: Mapped[Order] = relationship(back_populates="application")
    subscription: Mapped[Subscription] = relationship()


class NotificationDelivery(Base):
    __tablename__ = "notification_deliveries"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    subscription_id: Mapped[int] = mapped_column(
        ForeignKey("subscriptions.id"), nullable=False, index=True
    )
    notification_kind: Mapped[str] = mapped_column(String(64), nullable=False)
    delivery_key: Mapped[str] = mapped_column(String(128), nullable=False)
    delivery_bot_key: Mapped[str | None] = mapped_column(String(32), nullable=True, index=True)
    status: Mapped[str] = mapped_column(String(32), default="sending", nullable=False)
    attempt_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    last_error_code: Mapped[str | None] = mapped_column(String(64), nullable=True)
    claimed_at_utc: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    send_started_at_utc: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    provider_accepted_at_utc: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    delivered_at_utc: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    failed_at_utc: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    __table_args__ = (
        UniqueConstraint(
            "subscription_id",
            "notification_kind",
            "delivery_key",
            name="uq_notification_delivery",
        ),
    )


class AuditEvent(Base):
    __tablename__ = "audit_events"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    created_at_utc: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    event_type: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    telegram_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True, index=True)
    order_id: Mapped[int | None] = mapped_column(ForeignKey("orders.id"), nullable=True)
    subscription_id: Mapped[int | None] = mapped_column(
        ForeignKey("subscriptions.id"), nullable=True
    )
    public_guid: Mapped[str | None] = mapped_column(String(36), nullable=True)
    error_code: Mapped[str | None] = mapped_column(String(64), nullable=True)
    details_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    correlation_id: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    bot_key: Mapped[str | None] = mapped_column(String(32), nullable=True, index=True)


class AccessOperationLease(Base):
    __tablename__ = "access_operation_leases"

    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), primary_key=True, nullable=False)
    owner_kind: Mapped[str] = mapped_column(String(32), nullable=False)
    owner_key: Mapped[str] = mapped_column(String(128), nullable=False)
    lease_expires_at_utc: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    updated_at_utc: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class ProductEvent(Base):
    __tablename__ = "product_events"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    public_id: Mapped[str] = mapped_column(
        String(36), unique=True, nullable=False, default=lambda: str(uuid4())
    )
    user_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"), nullable=True, index=True)
    event_name: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    occurred_at_utc: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    correlation_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    idempotency_key: Mapped[str | None] = mapped_column(String(128), unique=True, nullable=True)
    payload_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    bot_key: Mapped[str | None] = mapped_column(String(32), nullable=True, index=True)


class CommercialEntitlementAdjustment(Base):
    __tablename__ = "commercial_entitlement_adjustments"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    subscription_id: Mapped[int] = mapped_column(
        ForeignKey("subscriptions.id"), nullable=False, index=True
    )
    source_kind: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    duration_delta_seconds: Mapped[int] = mapped_column(Integer, nullable=False)
    device_limit_before: Mapped[int] = mapped_column(Integer, nullable=False)
    device_limit_after: Mapped[int] = mapped_column(Integer, nullable=False)
    source_order_id: Mapped[int | None] = mapped_column(ForeignKey("orders.id"), nullable=True)
    source_entity_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    idempotency_key: Mapped[str] = mapped_column(String(128), nullable=False, unique=True)
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="applied", index=True)
    created_at_utc: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    applied_at_utc: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    reversed_by_adjustment_id: Mapped[int | None] = mapped_column(Integer, nullable=True)


class CommercialEntitlementSegment(Base):
    __tablename__ = "commercial_entitlement_segments"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    subscription_id: Mapped[int] = mapped_column(
        ForeignKey("subscriptions.id"), nullable=False, index=True
    )
    source_kind: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    starts_at_utc: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    ends_at_utc: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    source_order_id: Mapped[int | None] = mapped_column(ForeignKey("orders.id"), nullable=True)
    source_entity_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    idempotency_key: Mapped[str] = mapped_column(String(128), nullable=False, unique=True)
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="applied", index=True)
    created_at_utc: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    reversed_at_utc: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class OnboardingSession(Base):
    __tablename__ = "onboarding_sessions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), nullable=False, index=True)
    subscription_id: Mapped[int | None] = mapped_column(
        ForeignKey("subscriptions.id"), nullable=True, index=True
    )
    device_public_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    platform: Mapped[str | None] = mapped_column(String(32), nullable=True)
    current_step: Mapped[str] = mapped_column(String(64), nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    handoff_claim_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    issuance_request_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    origin_bot_key: Mapped[str | None] = mapped_column(String(32), nullable=True, index=True)
    created_at_utc: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    updated_at_utc: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    completed_at_utc: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    last_error_code: Mapped[str | None] = mapped_column(String(64), nullable=True)


class TrialClaim(Base):
    __tablename__ = "trial_claims"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(
        ForeignKey("users.id"), nullable=False, unique=True, index=True
    )
    subscription_id: Mapped[int | None] = mapped_column(
        ForeignKey("subscriptions.id"), nullable=True, index=True
    )
    status: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    duration_seconds: Mapped[int] = mapped_column(Integer, nullable=False)
    max_devices: Mapped[int] = mapped_column(Integer, nullable=False)
    started_at_utc: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    ends_at_utc: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    entitlement_version: Mapped[int | None] = mapped_column(Integer, nullable=True)
    idempotency_key: Mapped[str] = mapped_column(String(128), nullable=False, unique=True)
    created_at_utc: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    reserved_at_utc: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    usable_started_at_utc: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    activated_at_utc: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    converted_at_utc: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    expired_at_utc: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    revoked_at_utc: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    failure_code: Mapped[str | None] = mapped_column(String(64), nullable=True)
    activation_attempt_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    last_activation_attempt_at_utc: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )


class UserDiscount(Base):
    __tablename__ = "user_discounts"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), nullable=False, index=True)
    discount_bps: Mapped[int] = mapped_column(Integer, nullable=False)
    scope: Mapped[str] = mapped_column(String(32), nullable=False, default="all")
    starts_at_utc: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    expires_at_utc: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    max_uses: Mapped[int | None] = mapped_column(Integer, nullable=True)
    used_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_by_admin_telegram_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    created_at_utc: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    revoked_at_utc: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    revoked_by_admin_telegram_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)


class DiscountRedemption(Base):
    __tablename__ = "discount_redemptions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    discount_id: Mapped[int] = mapped_column(
        ForeignKey("user_discounts.id"), nullable=False, index=True
    )
    order_id: Mapped[int] = mapped_column(ForeignKey("orders.id"), nullable=False, unique=True)
    status: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    reserved_at_utc: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    applied_at_utc: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    released_at_utc: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    discount_amount_minor_units: Mapped[int] = mapped_column(Integer, nullable=False)


class ReferralReward(Base):
    __tablename__ = "referral_rewards"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    referrer_user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), nullable=False)
    referred_user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), nullable=False)
    source_order_id: Mapped[int] = mapped_column(
        ForeignKey("orders.id"), nullable=False, unique=True
    )
    reward_percent: Mapped[int] = mapped_column(Integer, nullable=False)
    reward_duration_seconds: Mapped[int] = mapped_column(Integer, nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    available_at_utc: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    target_subscription_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    entitlement_version: Mapped[int | None] = mapped_column(Integer, nullable=True)
    idempotency_key: Mapped[str] = mapped_column(String(128), nullable=False, unique=True)
    created_at_utc: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    applied_at_utc: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    cancelled_at_utc: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    reversed_at_utc: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    previous_entitlement_version: Mapped[int | None] = mapped_column(Integer, nullable=True)
    previous_status: Mapped[str | None] = mapped_column(String(32), nullable=True)
    previous_valid_until_utc: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    previous_max_devices: Mapped[int | None] = mapped_column(Integer, nullable=True)
    reversal_operation_id: Mapped[int | None] = mapped_column(
        ForeignKey("entitlement_operations.id"), nullable=True
    )
    failure_code: Mapped[str | None] = mapped_column(String(64), nullable=True)


class AcquisitionCampaign(Base):
    __tablename__ = "acquisition_campaigns"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    public_token: Mapped[str] = mapped_column(String(48), nullable=False, unique=True, index=True)
    channel: Mapped[str] = mapped_column(String(64), nullable=False)
    placement: Mapped[str | None] = mapped_column(String(128), nullable=True)
    creative: Mapped[str | None] = mapped_column(String(128), nullable=True)
    landing_variant: Mapped[str | None] = mapped_column(String(64), nullable=True)
    status: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    starts_at_utc: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    ends_at_utc: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    planned_spend_minor_units: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    actual_spend_minor_units: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    currency: Mapped[str] = mapped_column(String(8), nullable=False, default="RUB")
    created_at_utc: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    updated_at_utc: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class UserAcquisition(Base):
    __tablename__ = "user_acquisition"

    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), primary_key=True)
    first_campaign_id: Mapped[int | None] = mapped_column(
        ForeignKey("acquisition_campaigns.id"), nullable=True, index=True
    )
    first_touch_at_utc: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    first_bot_key: Mapped[str | None] = mapped_column(String(32), nullable=True)
    first_start_payload_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)
    last_campaign_id: Mapped[int | None] = mapped_column(
        ForeignKey("acquisition_campaigns.id"), nullable=True, index=True
    )
    last_touch_at_utc: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    last_bot_key: Mapped[str | None] = mapped_column(String(32), nullable=True)


class AcquisitionTouch(Base):
    __tablename__ = "acquisition_touches"
    __table_args__ = (
        UniqueConstraint(
            "user_id",
            "campaign_id",
            "touch_kind",
            "payload_hash",
            name="uq_acquisition_touch_evidence",
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), nullable=False, index=True)
    campaign_id: Mapped[int] = mapped_column(
        ForeignKey("acquisition_campaigns.id"), nullable=False, index=True
    )
    touched_at_utc: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    bot_key: Mapped[str | None] = mapped_column(String(32), nullable=True)
    touch_kind: Mapped[str] = mapped_column(String(32), nullable=False)
    payload_hash: Mapped[str] = mapped_column(String(64), nullable=False)


class CapacityStateTransition(Base):
    __tablename__ = "capacity_state_transitions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    previous_state: Mapped[str | None] = mapped_column(String(32), nullable=True)
    new_state: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    reason_code: Mapped[str] = mapped_column(String(64), nullable=False)
    snapshot_json: Mapped[str] = mapped_column(Text, nullable=False)
    captured_at_utc: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class SupportRequest(Base):
    __tablename__ = "support_requests"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    public_id: Mapped[str] = mapped_column(String(36), nullable=False, unique=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), nullable=False, index=True)
    category: Mapped[str] = mapped_column(String(64), nullable=False)
    origin_bot_key: Mapped[str | None] = mapped_column(String(32), nullable=True, index=True)
    status: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    created_at_utc: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    updated_at_utc: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    support_chat_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    support_root_message_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    closed_at_utc: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    closed_by_admin_telegram_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)


class SupportMessage(Base):
    __tablename__ = "support_messages"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    support_request_id: Mapped[int] = mapped_column(
        ForeignKey("support_requests.id"), nullable=False, index=True
    )
    direction: Mapped[str] = mapped_column(String(16), nullable=False)
    bot_key: Mapped[str | None] = mapped_column(String(32), nullable=True, index=True)
    telegram_message_id: Mapped[int] = mapped_column(Integer, nullable=False)
    telegram_chat_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    message_type: Mapped[str] = mapped_column(String(32), nullable=False)
    created_at_utc: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class WorkerHealth(Base):
    __tablename__ = "worker_health"

    worker_name: Mapped[str] = mapped_column(String(64), primary_key=True)
    last_attempt_at_utc: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    last_success_at_utc: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    last_failure_at_utc: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    last_error_code: Mapped[str | None] = mapped_column(String(64), nullable=True)
