from __future__ import annotations

import hashlib
from datetime import timedelta
from pathlib import Path

import pytest
from sqlalchemy import select

from vpn_access_bot.config import Settings
from vpn_access_bot.constants import (
    ORDER_STATUS_EXPIRED,
    ORDER_STATUS_PAYMENT_RECEIVED,
    ORDER_STATUS_PENDING,
    PAYMENT_MODE_TELEGRAM_STARS,
)
from vpn_access_bot.db import Database
from vpn_access_bot.models import Order, PaymentInbox, utc_now
from vpn_access_bot.payment_processing import PaymentEvidence, PaymentInboxIngestionService
from vpn_access_bot.product_catalog import ProductCatalog
from vpn_access_bot.product_completion import (
    expire_pending_orders_once,
    reconcile_payment_inbox_once,
)
from vpn_access_bot.repositories import OrderRepository, PaymentInboxRepository, to_aware_utc
from vpn_access_bot.services import PurchaseService


def _settings() -> Settings:
    return Settings(
        TELEGRAM_BOT_TOKEN="test-token",
        ADMIN_TELEGRAM_IDS="1",
        PAYMENT_MODE=PAYMENT_MODE_TELEGRAM_STARS,
        MEDIATOR_BASE_URL="http://127.0.0.1:5062",
        MEDIATOR_ADMIN_TOKEN="test-admin-token",
        CHECKOUT_AUTHORIZATION_GRACE_SECONDS=600,
    )


async def _create_order(database: Database, settings: Settings, telegram_id: int = 100) -> Order:
    async with database.session() as session:
        order = await PurchaseService(session, settings, object()).create_order_for_tariff(  # type: ignore[arg-type]
            telegram_id=telegram_id,
            username="payment-user",
            first_name="Payment",
            tariff_code="month_3_devices",
        )
        order.pricing_version = ProductCatalog.from_settings(settings).pricing_identity
        order_id = order.id

    async with database.session() as session:
        stored = await session.get(Order, order_id)
        assert stored is not None
        return stored


async def _ingest(
    database: Database,
    order: Order,
    *,
    charge_id: str,
    payer_telegram_id: int = 100,
    occurred_at=None,
    payment_bot_key: str = "primary",
) -> int:
    async with database.session() as session:
        inbox = await PaymentInboxIngestionService(session).ingest_telegram_stars(
            PaymentEvidence(
                invoice_payload=order.invoice_payload,
                amount_minor_units=order.amount_minor_units,
                currency=order.currency,
                provider_charge_id=charge_id,
                payer_telegram_id=payer_telegram_id,
                provider_occurred_at_utc=occurred_at,
                payment_bot_key=payment_bot_key,
            )
        )
        return inbox.id


@pytest.mark.asyncio
async def test_payment_inbox_survives_restart_and_reconciles_order(tmp_path: Path) -> None:
    database_path = tmp_path / "payment-restart.db"
    settings = _settings()
    database = Database(f"sqlite+aiosqlite:///{database_path}")
    await database.initialize()
    order = await _create_order(database, settings)
    inbox_id = await _ingest(database, order, charge_id="restart-charge")
    await database.dispose()

    reopened = Database(f"sqlite+aiosqlite:///{database_path}")
    await reopened.initialize()
    try:
        processed = await reconcile_payment_inbox_once(
            reopened.session_factory,
            object(),  # type: ignore[arg-type]
            settings,
        )
        assert processed == 1

        async with reopened.session() as session:
            stored_order = await session.get(Order, order.id)
            inbox = await session.get(PaymentInbox, inbox_id)
            assert stored_order is not None
            assert inbox is not None
            assert stored_order.status == ORDER_STATUS_PAYMENT_RECEIVED
            assert stored_order.provider_payment_id == "restart-charge"
            assert inbox.reconciliation_status == "matched"
            assert inbox.matched_order_id == order.id
    finally:
        await reopened.dispose()


@pytest.mark.asyncio
async def test_legacy_inbox_without_raw_payload_recovers_by_exact_owner_hash(
    tmp_path: Path,
) -> None:
    database = Database(f"sqlite+aiosqlite:///{tmp_path / 'legacy-payment-inbox.db'}")
    await database.initialize()
    try:
        settings = _settings()
        order = await _create_order(database, settings)
        now = utc_now()
        async with database.session() as session:
            legacy = PaymentInbox(
                provider=PAYMENT_MODE_TELEGRAM_STARS,
                provider_charge_id="legacy-charge",
                invoice_payload_hash=hashlib.sha256(
                    order.invoice_payload.encode("utf-8")
                ).hexdigest(),
                invoice_payload=None,
                payer_external_id="100",
                amount_minor_units=order.amount_minor_units,
                currency=order.currency,
                received_at_utc=now,
                reconciliation_status="received",
                attempt_count=0,
                updated_at_utc=now,
            )
            session.add(legacy)
            await session.flush()
            inbox_id = legacy.id

        processed = await reconcile_payment_inbox_once(
            database.session_factory,
            object(),  # type: ignore[arg-type]
            settings,
        )
        assert processed == 1

        async with database.session() as session:
            inbox = await session.get(PaymentInbox, inbox_id)
            stored_order = await session.get(Order, order.id)
            assert inbox is not None
            assert stored_order is not None
            assert inbox.invoice_payload == order.invoice_payload
            assert inbox.reconciliation_status == "matched"
            assert stored_order.status == ORDER_STATUS_PAYMENT_RECEIVED
    finally:
        await database.dispose()


@pytest.mark.asyncio
async def test_duplicate_charge_with_conflicting_evidence_requires_manual_review(
    tmp_path: Path,
) -> None:
    database = Database(f"sqlite+aiosqlite:///{tmp_path / 'payment-conflict.db'}")
    await database.initialize()
    try:
        settings = _settings()
        order = await _create_order(database, settings)
        inbox_id = await _ingest(database, order, charge_id="same-charge")

        async with database.session() as session:
            await PaymentInboxIngestionService(session).ingest_telegram_stars(
                PaymentEvidence(
                    invoice_payload=order.invoice_payload,
                    amount_minor_units=order.amount_minor_units + 1,
                    currency=order.currency,
                    provider_charge_id="same-charge",
                    payer_telegram_id=100,
                    origin_bot_key="primary",
                )
            )

        async with database.session() as session:
            inbox = await session.get(PaymentInbox, inbox_id)
            stored_order = await session.get(Order, order.id)
            assert inbox is not None
            assert stored_order is not None
            assert inbox.reconciliation_status == "manual_review"
            assert inbox.failure_code == "provider_charge_evidence_conflict"
            assert stored_order.status == ORDER_STATUS_PENDING
            assert stored_order.provider_payment_id is None
    finally:
        await database.dispose()


@pytest.mark.asyncio
async def test_checkout_authorization_prevents_expiration_race(tmp_path: Path) -> None:
    database = Database(f"sqlite+aiosqlite:///{tmp_path / 'checkout-race.db'}")
    await database.initialize()
    try:
        settings = _settings()
        order = await _create_order(database, settings)

        async with database.session() as session:
            valid, error = await PurchaseService(
                session,
                settings,
                object(),  # type: ignore[arg-type]
            ).validate_order_before_checkout(
                payload=order.invoice_payload,
                amount_minor_units=order.amount_minor_units,
                currency=order.currency,
                payer_telegram_id=100,
            )
            assert valid is True, error
            assert error is None
            stored_order = await session.get(Order, order.id)
            assert stored_order is not None
            stored_order.expires_at_utc = utc_now() - timedelta(seconds=1)

        async with database.session() as session:
            expired = await expire_pending_orders_once(session)
            assert expired == 0
            stored_order = await session.get(Order, order.id)
            assert stored_order is not None
            assert stored_order.status == ORDER_STATUS_PENDING
            assert stored_order.checkout_authorized_until_utc is not None
            assert to_aware_utc(stored_order.checkout_authorized_until_utc) > utc_now()
    finally:
        await database.dispose()


@pytest.mark.asyncio
async def test_expired_order_is_recovered_only_for_payment_inside_authorized_window(
    tmp_path: Path,
) -> None:
    database = Database(f"sqlite+aiosqlite:///{tmp_path / 'late-payment.db'}")
    await database.initialize()
    try:
        settings = _settings()
        accepted_order = await _create_order(database, settings, telegram_id=100)
        rejected_order = await _create_order(database, settings, telegram_id=101)
        now = utc_now()

        async with database.session() as session:
            for order_id in (accepted_order.id, rejected_order.id):
                order = await session.get(Order, order_id)
                assert order is not None
                order.status = ORDER_STATUS_EXPIRED
                order.checkout_authorized_at_utc = now - timedelta(minutes=2)
                order.checkout_authorized_until_utc = now + timedelta(minutes=2)

        accepted_id = await _ingest(
            database,
            accepted_order,
            charge_id="accepted-late-charge",
            payer_telegram_id=100,
            occurred_at=now,
        )
        rejected_id = await _ingest(
            database,
            rejected_order,
            charge_id="rejected-late-charge",
            payer_telegram_id=101,
            occurred_at=now + timedelta(minutes=3),
        )

        async with database.session() as session:
            service = PurchaseService(session, settings, object())  # type: ignore[arg-type]
            accepted = await service.reconcile_payment_inbox_by_id(accepted_id)
            rejected = await service.reconcile_payment_inbox_by_id(rejected_id)
            assert accepted.needs_activation is True
            assert rejected.failure_code == "late_payment_for_expired_order"

        async with database.session() as session:
            accepted_stored = await session.get(Order, accepted_order.id)
            rejected_stored = await session.get(Order, rejected_order.id)
            assert accepted_stored is not None
            assert rejected_stored is not None
            assert accepted_stored.status == ORDER_STATUS_PAYMENT_RECEIVED
            assert rejected_stored.status == ORDER_STATUS_EXPIRED
    finally:
        await database.dispose()


@pytest.mark.asyncio
async def test_payment_inbox_claim_is_exclusive_until_lease_expires(tmp_path: Path) -> None:
    database = Database(f"sqlite+aiosqlite:///{tmp_path / 'payment-claim.db'}")
    await database.initialize()
    try:
        settings = _settings()
        order = await _create_order(database, settings)
        await _ingest(database, order, charge_id="lease-charge")

        async with database.session() as session:
            first = await PaymentInboxRepository(session).claim_due(
                worker_id="worker-a",
                lease_seconds=60,
            )
            assert len(first) == 1

        async with database.session() as session:
            second = await PaymentInboxRepository(session).claim_due(
                worker_id="worker-b",
                lease_seconds=60,
            )
            assert second == []

        async with database.session() as session:
            inbox = (
                await session.execute(
                    select(PaymentInbox).where(PaymentInbox.provider_charge_id == "lease-charge")
                )
            ).scalar_one()
            inbox.claim_expires_at_utc = utc_now() - timedelta(seconds=1)

        async with database.session() as session:
            reclaimed = await PaymentInboxRepository(session).claim_due(
                worker_id="worker-b",
                lease_seconds=60,
            )
            assert len(reclaimed) == 1
            assert reclaimed[0].claimed_by == "worker-b"
            assert reclaimed[0].attempt_count == 2
    finally:
        await database.dispose()


class _OutboxBot:
    def __init__(self) -> None:
        self.calls: list[tuple[int, str, dict[str, object]]] = []

    async def send_message(self, recipient: int, text: str, **kwargs) -> None:
        self.calls.append((recipient, text, kwargs))


@pytest.mark.asyncio
async def test_order_activation_outbox_is_committed_and_not_sent_twice(tmp_path: Path) -> None:
    from vpn_access_bot.models import NotificationOutbox, User
    from vpn_access_bot.product_completion import dispatch_notification_outbox_once
    from vpn_access_bot.repositories import NotificationOutboxRepository

    database = Database(f"sqlite+aiosqlite:///{tmp_path / 'outbox.db'}")
    await database.initialize()
    bot = _OutboxBot()
    try:
        async with database.session() as session:
            user = User(
                telegram_id=700,
                username="outbox-user",
                first_name="Outbox",
                created_at=utc_now(),
                updated_at=utc_now(),
            )
            session.add(user)
            await session.flush()
            item = await NotificationOutboxRepository(session).enqueue_once(
                idempotency_key="order-activated:test",
                notification_kind="order_activated",
                user_id=user.id,
                bot_key="primary",
            )
            item_id = item.id

        first = await dispatch_notification_outbox_once(
            database.session_factory,
            bot,  # type: ignore[arg-type]
            _settings(),
        )
        second = await dispatch_notification_outbox_once(
            database.session_factory,
            bot,  # type: ignore[arg-type]
            _settings(),
        )

        assert first == 1
        assert second == 0
        assert len(bot.calls) == 1
        assert bot.calls[0][2]["bot_key"] == "primary"
        assert bot.calls[0][2]["reply_markup"] is not None

        async with database.session() as session:
            stored = await session.get(NotificationOutbox, item_id)
            assert stored is not None
            assert stored.state == "provider_accepted"
            assert stored.provider_accepted_at_utc is not None
    finally:
        await database.dispose()


@pytest.mark.asyncio
async def test_payment_bot_binding_is_immutable_across_bots(tmp_path: Path) -> None:
    database = Database(f"sqlite+aiosqlite:///{tmp_path / 'payment-bot-binding.db'}")
    await database.initialize()
    try:
        settings = _settings()
        order = await _create_order(database, settings)

        async with database.session() as session:
            stored = await session.get(Order, order.id)
            assert stored is not None
            assert await OrderRepository(session).claim_payment_bot(stored, "razakov") is True

        async with database.session() as session:
            valid, error = await PurchaseService(
                session,
                settings,
                object(),  # type: ignore[arg-type]
            ).validate_order_before_checkout(
                payload=order.invoice_payload,
                amount_minor_units=order.amount_minor_units,
                currency=order.currency,
                payer_telegram_id=100,
                payment_bot_key="razaltush",
            )
            assert valid is False
            assert error is not None
            assert "другим ботом" in error

        async with database.session() as session:
            valid, error = await PurchaseService(
                session,
                settings,
                object(),  # type: ignore[arg-type]
            ).validate_order_before_checkout(
                payload=order.invoice_payload,
                amount_minor_units=order.amount_minor_units,
                currency=order.currency,
                payer_telegram_id=100,
                payment_bot_key="razakov",
            )
            assert valid is True, error

        mismatched_id = await _ingest(
            database,
            order,
            charge_id="wrong-bot-charge",
            payment_bot_key="razaltush",
        )
        async with database.session() as session:
            outcome = await PurchaseService(
                session,
                settings,
                object(),  # type: ignore[arg-type]
            ).reconcile_payment_inbox_by_id(mismatched_id)
            assert outcome.failure_code == "payment_bot_mismatch"

        accepted_id = await _ingest(
            database,
            order,
            charge_id="correct-bot-charge",
            payment_bot_key="razakov",
        )
        async with database.session() as session:
            outcome = await PurchaseService(
                session,
                settings,
                object(),  # type: ignore[arg-type]
            ).reconcile_payment_inbox_by_id(accepted_id)
            assert outcome.needs_activation is True
            stored = await session.get(Order, order.id)
            assert stored is not None
            assert stored.payment_bot_key == "razakov"
            assert stored.provider_payment_id == "correct-bot-charge"
    finally:
        await database.dispose()
