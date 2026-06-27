from __future__ import annotations

import socket
from datetime import timedelta
from pathlib import Path

import aiohttp
import pytest
from sqlalchemy import text

from vpn_access_bot.checkout_tokens import CheckoutTokenCodec
from vpn_access_bot.checkout_web import CheckoutWebServer
from vpn_access_bot.commerce import PricingService
from vpn_access_bot.config import Settings
from vpn_access_bot.db import Database
from vpn_access_bot.models import utc_now
from vpn_access_bot.product_catalog import ProductCatalog
from vpn_access_bot.repositories import PurchaseQuoteRepository, UserRepository


class UnusedMediatorClient:
    pass


class UnusedReadiness:
    pass


class YooKassaSpy:
    create_calls = 0

    async def create_sbp_payment(self, **kwargs):
        _ = kwargs
        self.create_calls += 1
        raise AssertionError("GET must not create a provider payment")


def free_port() -> int:
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        return int(listener.getsockname()[1])


@pytest.mark.asyncio
async def test_checkout_get_has_no_financial_side_effect(tmp_path: Path) -> None:
    port = free_port()
    settings = Settings(
        TELEGRAM_BOT_TOKEN="test-token",
        MEDIATOR_ADMIN_TOKEN="test-admin-token",
        YOOKASSA_INTEGRATION_ENABLED=True,
        EXTERNAL_PAYMENT_ENABLED=True,
        CHECKOUT_PUBLIC_BASE_URL=f"http://127.0.0.1:{port}",
        CHECKOUT_BIND_HOST="127.0.0.1",
        CHECKOUT_BIND_PORT=port,
        CHECKOUT_TOKEN_SECRET="c" * 40,
        YOOKASSA_SHOP_ID="123456",
        YOOKASSA_SECRET_KEY="y" * 40,
        YOOKASSA_API_BASE_URL="https://api.test/v3",
        YOOKASSA_RETURN_URL=f"http://127.0.0.1:{port}/payment/return",
        YOOKASSA_WEBHOOK_PATH_SECRET="w" * 32,
    )
    database = Database(f"sqlite+aiosqlite:///{tmp_path / 'checkout.db'}")
    await database.initialize()
    spy = YooKassaSpy()
    server = CheckoutWebServer(
        settings=settings,
        database=database,
        mediator_client=UnusedMediatorClient(),  # type: ignore[arg-type]
        readiness=UnusedReadiness(),  # type: ignore[arg-type]
        yookassa=spy,  # type: ignore[arg-type]
    )
    try:
        async with database.session() as session:
            user = await UserRepository(session).get_or_create_from_message_user(
                601, "preview", "Preview"
            )
            quote = await PurchaseQuoteRepository(session).create(
                user=user,
                period_count=1,
                duration_days=30,
                max_devices=1,
                amount_minor_units=60,
                currency="XTR",
                pricing_version=ProductCatalog.from_settings(settings).pricing_identity,
                target_subscription_id=None,
                order_kind="purchase",
                expires_at=utc_now() + timedelta(minutes=20),
            )
            offer = PricingService(settings).calculate_quote_offer(quote, "yookassa_sbp")
            token = CheckoutTokenCodec("c" * 40).issue(
                quote.public_quote_id,
                int(quote.expires_at_utc.timestamp()),
                amount_minor_units=offer.amount_minor_units,
                pricing_version=offer.pricing_version,
            )

        await server.start()
        async with aiohttp.ClientSession() as client:
            response = await client.get(f"http://127.0.0.1:{port}/checkout/{token}")
            body = await response.text()
        async with database.session() as session:
            order_count = await session.execute(text("SELECT COUNT(*) FROM orders"))

        assert response.status == 200
        assert "Оплатить по СБП" in body
        assert spy.create_calls == 0
        assert order_count.scalar_one() == 0
    finally:
        await server.close()
        await database.dispose()
