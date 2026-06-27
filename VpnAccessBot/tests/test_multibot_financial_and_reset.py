from __future__ import annotations

import hashlib
from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import ClassVar

import pytest
from aiogram.exceptions import TelegramBadRequest
from pydantic import ValidationError
from sqlalchemy import select

from vpn_access_bot.config import Settings
from vpn_access_bot.constants import PAYMENT_MODE_TELEGRAM_STARS
from vpn_access_bot.db import Database
from vpn_access_bot.handlers.buy import _send_order_invoice
from vpn_access_bot.models import (
    AccessOperationLease,
    AuditEvent,
    Order,
    PaymentInbox,
    PurchaseQuote,
    Subscription,
    TelegramBotChannel,
    TrialClaim,
    User,
    UserBotChannel,
    UserDiscount,
    utc_now,
)
from vpn_access_bot.product_completion import user_has_paid_history
from vpn_access_bot.repositories import SubscriptionRepository, TrialClaimRepository
from vpn_access_bot.services import InvoicePrice, TelegramStarsInvoice
from vpn_access_bot.telegram.notification_sender import (
    NotificationRecipientUnavailable,
    NotificationSender,
)
from vpn_access_bot.test_user_reset import TestUserResetService as UserResetService
from vpn_access_bot.trial import TrialEligibilityReason, TrialEligibilityService


def _settings(**overrides: object) -> Settings:
    values: dict[str, object] = {
        "TELEGRAM_BOT_TOKEN": "test-token",
        "ADMIN_TELEGRAM_IDS": "100,200",
        "MEDIATOR_ADMIN_TOKEN": "test-admin-token",
        "PAYMENT_MODE": PAYMENT_MODE_TELEGRAM_STARS,
    }
    values.update(overrides)
    return Settings(**values)


class _InvoiceBot:
    def __init__(self) -> None:
        self.calls: list[dict[str, object]] = []

    async def send_invoice(self, **kwargs: object) -> None:
        self.calls.append(dict(kwargs))


class _InvoiceRegistry:
    def __init__(self, bots: dict[str, _InvoiceBot]) -> None:
        self._bots = bots

    def get(self, bot_key: str):
        bot = self._bots.get(bot_key)
        return None if bot is None else SimpleNamespace(bot=bot)


class _Message:
    def __init__(self) -> None:
        self.edits: list[str] = []

    async def edit_text(self, text: str, **_: object) -> None:
        self.edits.append(text)


@pytest.mark.asyncio
async def test_invoice_is_sent_by_payment_bot_not_current_callback_bot() -> None:
    payment_bot = _InvoiceBot()
    other_bot = _InvoiceBot()
    registry = _InvoiceRegistry({"payment": payment_bot, "other": other_bot})
    callback = SimpleNamespace(
        message=_Message(),
        from_user=SimpleNamespace(id=777),
    )
    order = Order(
        user_id=1,
        origin_bot_key="other",
        payment_bot_key="payment",
        status="pending",
        amount_minor_units=60,
        currency="XTR",
        provider=PAYMENT_MODE_TELEGRAM_STARS,
        invoice_payload="invoice-route-test",
        created_at=utc_now(),
    )
    invoice = TelegramStarsInvoice(
        title="VPN",
        description="Access",
        payload=order.invoice_payload,
        provider_token="",
        currency="XTR",
        prices=[InvoicePrice(label="VPN", amount=60)],
    )

    await _send_order_invoice(
        callback,
        order,
        invoice,
        _settings(),
        registry,  # type: ignore[arg-type]
    )

    assert len(payment_bot.calls) == 1
    assert payment_bot.calls[0]["chat_id"] == 777
    assert other_bot.calls == []
    assert callback.message.edits == []


class _DeliveryBot:
    def __init__(self, key: str) -> None:
        self.key = key
        self.calls: list[int] = []

    async def send_message(self, telegram_id: int, _text: str, **_: object):
        self.calls.append(telegram_id)
        return object()


class _ChatNotFoundDeliveryBot(_DeliveryBot):
    async def send_message(self, telegram_id: int, _text: str, **_: object):
        self.calls.append(telegram_id)
        raise TelegramBadRequest(
            method=object(),  # type: ignore[arg-type]
            message="Bad Request: chat not found",
        )


class _DeliveryRegistry:
    def __init__(self, bots: dict[str, _DeliveryBot]) -> None:
        self._runtimes = {key: SimpleNamespace(key=key, bot=bot) for key, bot in bots.items()}

    def delivery_candidates_for_keys(
        self,
        preferred_bot_keys: list[str],
        *,
        excluded_bot_keys: set[str] | None = None,
    ):
        excluded = excluded_bot_keys or set()
        ordered = []
        seen: set[str] = set()
        for key in [*preferred_bot_keys, *self._runtimes]:
            if key in seen or key in excluded or key not in self._runtimes:
                continue
            ordered.append(self._runtimes[key])
            seen.add(key)
        return ordered


@pytest.mark.asyncio
async def test_background_notification_falls_back_when_chat_is_missing_for_one_bot() -> None:
    missing = _ChatNotFoundDeliveryBot("razakov")
    fallback = _DeliveryBot("razaltush")
    sender = NotificationSender(
        _DeliveryRegistry({"razakov": missing, "razaltush": fallback}),  # type: ignore[arg-type]
        default_bot_key="razakov",
    )

    result = await sender.send_message(777, "hello")

    assert result.delivery_bot_key == "razaltush"
    assert missing.calls == [777]
    assert fallback.calls == [777]


@pytest.mark.asyncio
async def test_background_notification_reports_terminal_recipient_unavailability() -> None:
    sender = NotificationSender(
        _DeliveryRegistry({}),  # type: ignore[arg-type]
        default_bot_key="razakov",
    )

    with pytest.raises(NotificationRecipientUnavailable):
        await sender.send_message(777, "hello")


@pytest.mark.asyncio
async def test_background_notification_prefers_last_active_unblocked_bot(tmp_path: Path) -> None:
    database = Database(f"sqlite+aiosqlite:///{tmp_path / 'notifications.db'}")
    await database.initialize()
    razakov = _DeliveryBot("razakov")
    razaltush = _DeliveryBot("razaltush")
    try:
        now = utc_now()
        async with database.session() as session:
            user = User(
                telegram_id=777,
                username="multi",
                first_name="Multi",
                created_at=now,
                updated_at=now,
            )
            session.add(user)
            session.add_all(
                [
                    TelegramBotChannel(
                        bot_key="razakov",
                        telegram_bot_id=1001,
                        username="RazakovVpnBot",
                        enabled=True,
                        required=True,
                        last_verified_at_utc=now,
                    ),
                    TelegramBotChannel(
                        bot_key="razaltush",
                        telegram_bot_id=1002,
                        username="RazaltushVpnBot",
                        enabled=True,
                        required=True,
                        last_verified_at_utc=now,
                    ),
                ]
            )
            await session.flush()
            session.add_all(
                [
                    UserBotChannel(
                        user_id=user.id,
                        bot_key="razakov",
                        first_seen_at_utc=now - timedelta(days=2),
                        last_seen_at_utc=now - timedelta(days=1),
                        can_receive_messages=True,
                    ),
                    UserBotChannel(
                        user_id=user.id,
                        bot_key="razaltush",
                        first_seen_at_utc=now - timedelta(days=1),
                        last_seen_at_utc=now,
                        can_receive_messages=True,
                    ),
                ]
            )

        sender = NotificationSender(
            _DeliveryRegistry({"razakov": razakov, "razaltush": razaltush}),  # type: ignore[arg-type]
            database.session,
            "razakov",
        )
        result = await sender.send_message(777, "hello")
        assert result.delivery_bot_key == "razaltush"
        assert razaltush.calls == [777]
        assert razakov.calls == []

        async with database.session() as session:
            channel = await session.get(UserBotChannel, (1, "razaltush"))
            assert channel is not None
            channel.can_receive_messages = False
            channel.blocked_at_utc = utc_now()

        result = await sender.send_message(777, "fallback")
        assert result.delivery_bot_key == "razakov"
        assert razakov.calls == [777]
    finally:
        await database.dispose()


@pytest.mark.asyncio
async def test_test_user_reset_finalize_rejects_new_access_lease(tmp_path: Path) -> None:
    database = Database(f"sqlite+aiosqlite:///{tmp_path / 'reset-race.db'}")
    await database.initialize()

    try:
        now = utc_now()
        async with database.session() as session:
            session.add(
                User(
                    telegram_id=100,
                    username="test-admin",
                    first_name="Test",
                    created_at=now,
                    updated_at=now,
                )
            )

        async with database.session() as session:
            plan = await UserResetService(session).prepare(
                100,
                actor_telegram_id=100,
                source_request_id="test-reset-race:1",
            )
            assert plan.completed_outcome is None

        async with database.session() as session:
            user = (await session.execute(select(User).where(User.telegram_id == 100))).scalar_one()
            session.add(
                AccessOperationLease(
                    user_id=user.id,
                    owner_kind="trial",
                    owner_key="parallel-trial",
                    lease_expires_at_utc=now + timedelta(minutes=5),
                    updated_at_utc=now,
                )
            )

        async with database.session() as session:
            with pytest.raises(
                ValueError,
                match="test_user_reset_has_active_access_lease",
            ):
                await UserResetService(session).finalize(
                    100,
                    actor_telegram_id=100,
                    source_request_id="test-reset-race:1",
                )

        async with database.session() as session:
            user = (await session.execute(select(User).where(User.telegram_id == 100))).scalar_one()
            assert user.test_user_reset_generation == 0
            assert user.test_user_reset_at_utc is None
    finally:
        await database.dispose()


def test_test_user_reset_requires_explicit_admin_allowlist() -> None:
    with pytest.raises(ValidationError):
        _settings(ALLOW_TEST_USER_RESET=True, TEST_USER_RESET_TELEGRAM_IDS="")

    with pytest.raises(ValidationError):
        _settings(ALLOW_TEST_USER_RESET=True, TEST_USER_RESET_TELEGRAM_IDS="999")

    settings = _settings(
        ALLOW_TEST_USER_RESET=True,
        TEST_USER_RESET_TELEGRAM_IDS="100",
    )
    assert settings.allow_test_user_reset is True
    assert settings.test_user_reset_telegram_ids == [100]


@pytest.mark.asyncio
async def test_test_user_reset_archives_access_but_preserves_financial_history(
    tmp_path: Path,
) -> None:
    database = Database(f"sqlite+aiosqlite:///{tmp_path / 'test-reset.db'}")
    await database.initialize()
    try:
        now = utc_now()
        async with database.session() as session:
            user = User(
                telegram_id=100,
                username="test-admin",
                first_name="Test",
                platform_preference="android",
                created_at=now,
                updated_at=now,
            )
            session.add(user)
            await session.flush()
            subscription = Subscription(
                user_id=user.id,
                public_guid="00000000-0000-0000-0000-000000000100",
                signed_url="https://example.test/sub",
                max_devices=1,
                status="disabled",
                starts_at=now - timedelta(days=1),
                expires_at=now + timedelta(days=29),
                created_at=now,
                updated_at_utc=now,
                disabled_at=now,
            )
            session.add(subscription)
            await session.flush()
            user.primary_subscription_id = subscription.id

            paid_order = Order(
                user_id=user.id,
                origin_bot_key="razakov",
                payment_bot_key="razaltush",
                status="paid",
                amount_minor_units=60,
                currency="XTR",
                provider=PAYMENT_MODE_TELEGRAM_STARS,
                provider_payment_id="paid-charge",
                invoice_payload="paid-reset-order",
                created_at=now,
                paid_at=now,
            )
            pending_order = Order(
                user_id=user.id,
                origin_bot_key="razakov",
                status="pending",
                amount_minor_units=60,
                currency="XTR",
                provider=PAYMENT_MODE_TELEGRAM_STARS,
                invoice_payload="pending-reset-order",
                created_at=now,
                expires_at_utc=now + timedelta(minutes=30),
            )
            session.add_all([paid_order, pending_order])
            await session.flush()
            session.add(
                PaymentInbox(
                    provider=PAYMENT_MODE_TELEGRAM_STARS,
                    provider_charge_id="paid-charge",
                    invoice_payload_hash=hashlib.sha256(
                        paid_order.invoice_payload.encode("utf-8")
                    ).hexdigest(),
                    invoice_payload=paid_order.invoice_payload,
                    payer_external_id=str(user.telegram_id),
                    amount_minor_units=60,
                    currency="XTR",
                    received_at_utc=now,
                    origin_bot_key="razaltush",
                    payment_bot_key="razaltush",
                    matched_order_id=paid_order.id,
                    reconciliation_status="applied",
                    attempt_count=1,
                    processed_at_utc=now,
                    updated_at_utc=now,
                )
            )
            session.add(
                PurchaseQuote(
                    user_id=user.id,
                    origin_bot_key="razakov",
                    period_count=1,
                    duration_days=30,
                    max_devices=1,
                    amount_minor_units=60,
                    currency="XTR",
                    pricing_version="test",
                    order_kind="purchase",
                    expires_at_utc=now + timedelta(minutes=20),
                    created_at_utc=now,
                )
            )
            session.add(
                UserDiscount(
                    user_id=user.id,
                    discount_bps=10000,
                    scope="purchase",
                    starts_at_utc=now,
                    expires_at_utc=now + timedelta(days=1),
                    max_uses=1,
                    used_count=0,
                    status="active",
                    reason="test",
                    created_by_admin_telegram_id=100,
                    created_at_utc=now,
                )
            )
            session.add(
                TrialClaim(
                    user_id=user.id,
                    subscription_id=subscription.id,
                    status="expired",
                    duration_seconds=3600,
                    max_devices=1,
                    idempotency_key="test-reset-trial",
                    created_at_utc=now,
                )
            )

        async with database.session() as session:
            plan = await UserResetService(session).prepare(
                100,
                actor_telegram_id=100,
                source_request_id="test-reset:1",
            )
            assert plan.subscription_public_guids_to_disable == ()
            outcome = await UserResetService(session).finalize(
                100,
                actor_telegram_id=100,
                source_request_id="test-reset:1",
            )
            assert outcome.archived_subscriptions == 1
            assert outcome.cancelled_orders == 1
            assert outcome.consumed_quotes == 1
            assert outcome.revoked_discounts == 1
            assert outcome.removed_trial_claims == 1

        async with database.session() as session:
            replay_plan = await UserResetService(session).prepare(
                100,
                actor_telegram_id=100,
                source_request_id="test-reset:1",
            )
            assert replay_plan.completed_outcome == outcome
            replay_outcome = await UserResetService(session).finalize(
                100,
                actor_telegram_id=100,
                source_request_id="test-reset:1",
            )
            assert replay_outcome == outcome

        async with database.session() as session:
            stored_user = (
                await session.execute(select(User).where(User.telegram_id == 100))
            ).scalar_one()
            assert stored_user.primary_subscription_id is None
            assert stored_user.platform_preference is None
            assert stored_user.test_user_reset_generation == 1
            assert stored_user.test_user_reset_at_utc is not None
            assert await SubscriptionRepository(session).list_visible_for_user(stored_user.id) == []
            assert await user_has_paid_history(session, stored_user.id) is True

            eligibility = await TrialEligibilityService(session, _settings()).evaluate(
                stored_user,
                None,
            )
            assert eligibility.reason == TrialEligibilityReason.AVAILABLE
            assert eligibility.is_available is True

            claim, acquired = await TrialClaimRepository(session).acquire_activation(
                stored_user,
                duration_seconds=2 * 86400,
                max_devices=1,
            )
            assert acquired is True
            assert claim is not None
            assert claim.idempotency_key == f"trial:{stored_user.id}:reset:1"

            orders = list(
                (
                    await session.execute(
                        select(Order).where(Order.user_id == stored_user.id).order_by(Order.id)
                    )
                ).scalars()
            )
            assert [order.status for order in orders] == ["paid", "cancelled"]
            assert orders[0].provider_payment_id == "paid-charge"

            inbox = (
                await session.execute(
                    select(PaymentInbox).where(PaymentInbox.provider_charge_id == "paid-charge")
                )
            ).scalar_one()
            assert inbox.reconciliation_status == "applied"
            assert inbox.payment_bot_key == "razaltush"

            audit = (
                await session.execute(
                    select(AuditEvent).where(AuditEvent.event_type == "test.user_reset_completed")
                )
            ).scalar_one()
            assert audit.telegram_id == 100
    finally:
        await database.dispose()


class _RefundBot:
    def __init__(self) -> None:
        self.calls: list[tuple[int, str]] = []

    async def refund_star_payment(self, *, user_id: int, telegram_payment_charge_id: str) -> None:
        self.calls.append((user_id, telegram_payment_charge_id))


class _SessionContext:
    async def __aenter__(self):
        return object()

    async def __aexit__(self, *_: object) -> None:
        return None


class _FakeDatabase:
    def session(self):
        return _SessionContext()


class _RefundPurchaseService:
    completed: ClassVar[list[int]] = []

    def __init__(self, _session: object, _settings: object, _mediator: object) -> None:
        pass

    async def confirm_refund(self, token: str, *, admin_telegram_id: int | None):
        assert token == "confirm-token"
        assert admin_telegram_id == 100
        return SimpleNamespace(
            already_refunded=False,
            is_eligible=True,
            charge_id="stars-charge",
            reason=None,
            order=SimpleNamespace(
                id=42,
                payment_bot_key="payment",
                origin_bot_key="origin",
                user=SimpleNamespace(telegram_id=777),
            ),
        )

    async def complete_refund_after_provider(self, order_id: int) -> None:
        self.completed.append(order_id)


class _AdminMessage:
    def __init__(self, current_bot: _RefundBot) -> None:
        self.text = "/confirm_refund confirm-token"
        self.from_user = SimpleNamespace(id=100)
        self.bot = current_bot
        self.answers: list[str] = []

    async def answer(self, text: str, **_: object) -> None:
        self.answers.append(text)


@pytest.mark.asyncio
async def test_refund_uses_actual_payment_bot(monkeypatch: pytest.MonkeyPatch) -> None:
    from vpn_access_bot.handlers import admin

    _RefundPurchaseService.completed = []
    monkeypatch.setattr(admin, "PurchaseService", _RefundPurchaseService)
    current_bot = _RefundBot()
    payment_bot = _RefundBot()
    origin_bot = _RefundBot()
    message = _AdminMessage(current_bot)
    registry = _InvoiceRegistry(
        {"payment": payment_bot, "origin": origin_bot}  # type: ignore[arg-type]
    )

    await admin.handle_confirm_refund(
        message,  # type: ignore[arg-type]
        _FakeDatabase(),  # type: ignore[arg-type]
        _settings(),
        object(),  # type: ignore[arg-type]
        registry,  # type: ignore[arg-type]
    )

    assert payment_bot.calls == [(777, "stars-charge")]
    assert origin_bot.calls == []
    assert current_bot.calls == []
    assert _RefundPurchaseService.completed == [42]
    assert any("выполнен возврат" in answer for answer in message.answers)
