from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import json
import time
from dataclasses import dataclass


class CheckoutTokenError(ValueError):
    pass


@dataclass(frozen=True)
class CheckoutTokenClaims:
    quote_id: str
    expires_at_unix: int
    amount_minor_units: int | None
    pricing_version: str | None


class CheckoutTokenCodec:
    def __init__(self, secret: str) -> None:
        normalized = secret.strip()
        if len(normalized.encode("utf-8")) < 32:
            raise ValueError("checkout_token_secret_too_short")
        self._secret = normalized.encode("utf-8")

    def issue(
        self,
        quote_id: str,
        expires_at_unix: int,
        *,
        amount_minor_units: int | None = None,
        pricing_version: str | None = None,
    ) -> str:
        if not quote_id or expires_at_unix <= 0:
            raise ValueError("invalid_checkout_token_claims")
        payload = json.dumps(
            {
                "q": quote_id,
                "exp": expires_at_unix,
                "a": amount_minor_units,
                "p": pricing_version,
                "v": 1,
            },
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        encoded = self._encode(payload)
        signature = hmac.new(self._secret, encoded.encode("ascii"), hashlib.sha256).digest()
        return f"{encoded}.{self._encode(signature)}"

    def verify(self, token: str, now_unix: int | None = None) -> CheckoutTokenClaims:
        try:
            encoded, supplied_signature = token.split(".", maxsplit=1)
            expected = hmac.new(self._secret, encoded.encode("ascii"), hashlib.sha256).digest()
            actual = self._decode(supplied_signature)
            if not hmac.compare_digest(actual, expected):
                raise CheckoutTokenError("checkout_token_invalid")
            payload = json.loads(self._decode(encoded))
            quote_id = str(payload["q"])
            expires_at = int(payload["exp"])
            version = int(payload["v"])
            amount = int(payload["a"]) if payload.get("a") is not None else None
            pricing = str(payload["p"]) if payload.get("p") is not None else None
        except (
            KeyError,
            TypeError,
            ValueError,
            binascii.Error,
            json.JSONDecodeError,
        ) as exception:
            raise CheckoutTokenError("checkout_token_invalid") from exception
        if version != 1 or not quote_id:
            raise CheckoutTokenError("checkout_token_invalid")
        if expires_at <= int(time.time() if now_unix is None else now_unix):
            raise CheckoutTokenError("checkout_token_expired")
        if amount is not None and amount <= 0:
            raise CheckoutTokenError("checkout_token_invalid")
        return CheckoutTokenClaims(
            quote_id=quote_id,
            expires_at_unix=expires_at,
            amount_minor_units=amount,
            pricing_version=pricing,
        )

    @staticmethod
    def _encode(value: bytes) -> str:
        return base64.urlsafe_b64encode(value).decode("ascii").rstrip("=")

    @staticmethod
    def _decode(value: str) -> bytes:
        return base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))
