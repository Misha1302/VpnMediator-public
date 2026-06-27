from __future__ import annotations

from datetime import UTC, datetime

import pytest
from aiohttp import web

from vpn_access_bot.checkout_web import CheckoutWebServer
from vpn_access_bot.yookassa import YooKassaError, YooKassaPayment


def test_checkout_page_is_non_cacheable_and_not_frameable() -> None:
    response = CheckoutWebServer._page("Оплата", "<p>Тест</p>")

    assert response.headers["Cache-Control"] == "no-store"
    assert response.headers["X-Frame-Options"] == "DENY"
    assert "frame-ancestors 'none'" in response.headers["Content-Security-Policy"]
    assert "Разовый" not in response.text


def test_provider_redirect_requires_https() -> None:
    with pytest.raises(YooKassaError, match="confirmation_url_invalid"):
        CheckoutWebServer._redirect_to_provider("http://attacker.example/payment")

    with pytest.raises(web.HTTPSeeOther) as redirect:
        CheckoutWebServer._redirect_to_provider("https://yookassa.example/payment")
    assert redirect.value.location == "https://yookassa.example/payment"


def test_verified_provider_status_overrides_untrusted_webhook_event() -> None:
    payment = YooKassaPayment(
        payment_id="22d6d597-000f-5000-9000-145f6df21d6f",
        status="succeeded",
        amount_minor_units=19900,
        currency="RUB",
        confirmation_url=None,
        order_id="order-1",
        paid=True,
        created_at=datetime.now(UTC),
    )

    assert CheckoutWebServer._verified_payment_action(payment) == "succeeded"
