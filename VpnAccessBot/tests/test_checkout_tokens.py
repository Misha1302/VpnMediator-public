from __future__ import annotations

import pytest

from vpn_access_bot.checkout_tokens import CheckoutTokenCodec, CheckoutTokenError


def test_checkout_token_round_trip_and_expiry() -> None:
    codec = CheckoutTokenCodec("s" * 32)
    token = codec.issue(
        "quote-123",
        expires_at_unix=2_000,
        amount_minor_units=19900,
        pricing_version="v1:rub",
    )

    claims = codec.verify(token, now_unix=1_999)

    assert claims.quote_id == "quote-123"
    assert claims.amount_minor_units == 19900
    assert claims.pricing_version == "v1:rub"
    with pytest.raises(CheckoutTokenError, match="checkout_token_expired"):
        codec.verify(token, now_unix=2_000)


def test_checkout_token_rejects_tampering() -> None:
    codec = CheckoutTokenCodec("s" * 32)
    token = codec.issue("quote-123", expires_at_unix=2_000)
    payload, signature = token.split(".")

    with pytest.raises(CheckoutTokenError, match="checkout_token_invalid"):
        codec.verify(f"{payload}x.{signature}", now_unix=1_000)


def test_checkout_token_requires_strong_secret() -> None:
    with pytest.raises(ValueError, match="checkout_token_secret_too_short"):
        CheckoutTokenCodec("short")


@pytest.mark.parametrize("token", ["", "not-a-token", "@@@.@@@", "a.b.c"])
def test_checkout_token_rejects_malformed_input(token: str) -> None:
    codec = CheckoutTokenCodec("s" * 32)
    with pytest.raises(CheckoutTokenError, match="checkout_token_invalid"):
        codec.verify(token, now_unix=1_000)
