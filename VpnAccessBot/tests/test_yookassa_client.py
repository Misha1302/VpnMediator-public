from __future__ import annotations

import json

import httpx
import pytest

from vpn_access_bot.yookassa import YooKassaClient, YooKassaError


@pytest.mark.asyncio
async def test_create_sbp_payment_uses_idempotence_and_expected_contract() -> None:
    captured: dict[str, object] = {}

    async def handler(request: httpx.Request) -> httpx.Response:
        captured["authorization"] = request.headers.get("Authorization")
        captured["idempotence"] = request.headers.get("Idempotence-Key")
        captured["payload"] = json.loads(request.content)
        return httpx.Response(
            200,
            json={
                "id": "payment-1",
                "status": "pending",
                "paid": False,
                "amount": {"value": "199.00", "currency": "RUB"},
                "confirmation": {
                    "type": "redirect",
                    "confirmation_url": "https://yookassa.example/confirm",
                },
                "metadata": {"order_id": "order-1"},
            },
        )

    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(transport=transport, auth=("shop", "secret")) as http:
        client = YooKassaClient("shop", "secret", base_url="https://api.test/v3", client=http)
        payment = await client.create_sbp_payment(
            order_id="order-1",
            amount_minor_units=19900,
            return_url="https://pay.example/return",
            idempotence_key="stable-key",
            description="VPN",
        )

    assert captured["idempotence"] == "stable-key"
    assert str(captured["authorization"]).startswith("Basic ")
    assert captured["payload"]["payment_method_data"] == {"type": "sbp"}  # type: ignore[index]
    assert payment.payment_id == "payment-1"
    assert payment.amount_minor_units == 19900
    assert payment.order_id == "order-1"


@pytest.mark.asyncio
async def test_yookassa_http_failure_is_not_treated_as_payment() -> None:
    transport = httpx.MockTransport(lambda _: httpx.Response(401, json={"type": "error"}))
    async with httpx.AsyncClient(transport=transport) as http:
        client = YooKassaClient("shop", "secret", base_url="https://api.test/v3", client=http)
        with pytest.raises(YooKassaError, match="yookassa_http_401"):
            await client.get_payment("22d6d597-000f-5000-9000-145f6df21d6f")


@pytest.mark.asyncio
async def test_get_payment_rejects_path_injection_before_request() -> None:
    calls = 0

    async def handler(_: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(200, json={})

    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(transport=transport) as http:
        client = YooKassaClient("shop", "secret", base_url="https://api.test/v3", client=http)
        with pytest.raises(YooKassaError, match="payment_id_invalid"):
            await client.get_payment("../refunds?limit=100")

    assert calls == 0
