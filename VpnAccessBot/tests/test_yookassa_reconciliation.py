from __future__ import annotations

from datetime import timedelta
from pathlib import Path

import pytest

from vpn_access_bot.config import Settings
from vpn_access_bot.db import Database
from vpn_access_bot.models import utc_now
from vpn_access_bot.payment_processing import PaymentEvidence, PaymentInboxIngestionService
from vpn_access_bot.product_catalog import ProductCatalog
from vpn_access_bot.repositories import OrderRepository, PurchaseQuoteRepository, UserRepository
from vpn_access_bot.services import PurchaseService


class UnusedMediatorClient:
    pass


def settings() -> Settings:
    return Settings(
        TELEGRAM_BOT_TOKEN="test-token",
        PAYMENT_MODE="telegram_stars",
        MEDIATOR_ADMIN_TOKEN="test-admin-token",
        EXTERNAL_PAYMENT_ENABLED=True,
        YOOKASSA_INTEGRATION_ENABLED=True,
        CHECKOUT_PUBLIC_BASE_URL="https://pay.example.test",
        CHECKOUT_TOKEN_SECRET="c" * 40,
        YOOKASSA_SHOP_ID="123456",
        YOOKASSA_SECRET_KEY="y" * 40,
        YOOKASSA_RETURN_URL="https://pay.example.test/return",
        YOOKASSA_WEBHOOK_PATH_SECRET="w" * 32,
    )


@pytest.mark.asyncio
async def test_verified_yookassa_evidence_moves_order_to_payment_received(tmp_path: Path) -> None:
    database = Database(f"sqlite+aiosqlite:///{tmp_path / 'yookassa.db'}")
    await database.initialize()
    config = settings()
    try:
        async with database.session() as session:
            user = await UserRepository(session).get_or_create_from_message_user(
                501, "sbp-user", "SBP"
            )
            quote = await PurchaseQuoteRepository(session).create(
                user=user,
                period_count=1,
                duration_days=30,
                max_devices=1,
                amount_minor_units=60,
                currency="XTR",
                pricing_version=ProductCatalog.from_settings(config).pricing_identity,
                target_subscription_id=None,
                order_kind="purchase",
                expires_at=utc_now() + timedelta(minutes=20),
            )
            quote_id = quote.public_quote_id

        async with database.session() as session:
            order = await PurchaseService(
                session,
                config,
                UnusedMediatorClient(),  # type: ignore[arg-type]
            ).create_order_from_quote(
                quote_id,
                actor_telegram_id=501,
                payment_provider="yookassa_sbp",
            )
            public_order_id = order.public_order_id
            inbox = await PaymentInboxIngestionService(session).ingest_yookassa_sbp(
                PaymentEvidence(
                    invoice_payload=order.invoice_payload,
                    amount_minor_units=19900,
                    currency="RUB",
                    provider_charge_id="yk-payment-1",
                    payer_telegram_id=501,
                    provider_occurred_at_utc=utc_now(),
                )
            )
            inbox_id = inbox.id

        async with database.session() as session:
            result = await PurchaseService(
                session,
                config,
                UnusedMediatorClient(),  # type: ignore[arg-type]
            ).reconcile_payment_inbox_by_id(inbox_id)
            assert result.needs_activation is True

        async with database.session() as session:
            persisted = await OrderRepository(session).get_by_public_id(public_order_id)
            assert persisted is not None
            assert persisted.status == "payment_received"
            assert persisted.provider_payment_id == "yk-payment-1"
    finally:
        await database.dispose()


@pytest.mark.asyncio
async def test_yookassa_amount_mismatch_goes_to_manual_review(tmp_path: Path) -> None:
    database = Database(f"sqlite+aiosqlite:///{tmp_path / 'mismatch.db'}")
    await database.initialize()
    config = settings()
    try:
        async with database.session() as session:
            user = await UserRepository(session).get_or_create_from_message_user(502, None, "SBP")
            quote = await PurchaseQuoteRepository(session).create(
                user=user,
                period_count=1,
                duration_days=30,
                max_devices=1,
                amount_minor_units=60,
                currency="XTR",
                pricing_version=ProductCatalog.from_settings(config).pricing_identity,
                target_subscription_id=None,
                order_kind="purchase",
                expires_at=utc_now() + timedelta(minutes=20),
            )
            quote_id = quote.public_quote_id

        async with database.session() as session:
            order = await PurchaseService(
                session,
                config,
                UnusedMediatorClient(),  # type: ignore[arg-type]
            ).create_order_from_quote(
                quote_id, actor_telegram_id=502, payment_provider="yookassa_sbp"
            )
            inbox = await PaymentInboxIngestionService(session).ingest_yookassa_sbp(
                PaymentEvidence(
                    invoice_payload=order.invoice_payload,
                    amount_minor_units=1,
                    currency="RUB",
                    provider_charge_id="yk-mismatch",
                    payer_telegram_id=502,
                )
            )
            inbox_id = inbox.id

        async with database.session() as session:
            result = await PurchaseService(
                session,
                config,
                UnusedMediatorClient(),  # type: ignore[arg-type]
            ).reconcile_payment_inbox_by_id(inbox_id)
            assert result.inbox_status == "manual_review"
            assert result.failure_code == "payment_evidence_mismatch"
    finally:
        await database.dispose()
