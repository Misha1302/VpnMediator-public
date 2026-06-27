from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from typing import Any

import httpx


class YooKassaError(RuntimeError):
    pass


@dataclass(frozen=True)
class YooKassaPayment:
    payment_id: str
    status: str
    amount_minor_units: int
    currency: str
    confirmation_url: str | None
    order_id: str | None
    paid: bool
    created_at: datetime | None


class YooKassaClient:
    def __init__(
        self,
        shop_id: str,
        secret_key: str,
        *,
        base_url: str = "https://api.yookassa.ru/v3",
        timeout_seconds: float = 15.0,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        if not shop_id.strip() or not secret_key.strip():
            raise ValueError("yookassa_credentials_missing")
        self._base_url = base_url.rstrip("/")
        self._client = client or httpx.AsyncClient(
            auth=(shop_id.strip(), secret_key.strip()),
            timeout=timeout_seconds,
            headers={"Accept": "application/json"},
        )
        self._owns_client = client is None

    async def close(self) -> None:
        if self._owns_client:
            await self._client.aclose()

    async def create_sbp_payment(
        self,
        *,
        order_id: str,
        amount_minor_units: int,
        return_url: str,
        idempotence_key: str,
        description: str,
    ) -> YooKassaPayment:
        if amount_minor_units <= 0:
            raise ValueError("yookassa_amount_must_be_positive")
        try:
            response = await self._client.post(
                f"{self._base_url}/payments",
                headers={"Idempotence-Key": idempotence_key},
                json={
                    "amount": {
                        "value": self._format_amount(amount_minor_units),
                        "currency": "RUB",
                    },
                    "payment_method_data": {"type": "sbp"},
                    "confirmation": {"type": "redirect", "return_url": return_url},
                    "capture": True,
                    "description": description[:128],
                    "metadata": {"order_id": order_id},
                },
            )
        except httpx.HTTPError as exception:
            raise YooKassaError("yookassa_transport_error") from exception
        return self._parse_response(response)

    async def get_payment(self, payment_id: str) -> YooKassaPayment:
        if (
            re.fullmatch(
                r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}",
                payment_id,
            )
            is None
        ):
            raise YooKassaError("yookassa_payment_id_invalid")
        try:
            response = await self._client.get(f"{self._base_url}/payments/{payment_id}")
        except httpx.HTTPError as exception:
            raise YooKassaError("yookassa_transport_error") from exception
        return self._parse_response(response)

    @classmethod
    def _parse_response(cls, response: httpx.Response) -> YooKassaPayment:
        if response.status_code < 200 or response.status_code >= 300:
            raise YooKassaError(f"yookassa_http_{response.status_code}")
        try:
            payload: dict[str, Any] = response.json()
            amount = payload["amount"]
            payment_id = str(payload["id"])
            status = str(payload["status"])
            currency = str(amount["currency"]).upper()
            minor_units = cls._parse_amount(str(amount["value"]))
            confirmation = payload.get("confirmation") or {}
            metadata = payload.get("metadata") or {}
            created_at_raw = payload.get("captured_at") or payload.get("created_at")
            created_at = (
                datetime.fromisoformat(str(created_at_raw).replace("Z", "+00:00")).astimezone(UTC)
                if created_at_raw
                else None
            )
        except (KeyError, TypeError, ValueError) as exception:
            raise YooKassaError("yookassa_response_invalid") from exception
        return YooKassaPayment(
            payment_id=payment_id,
            status=status,
            amount_minor_units=minor_units,
            currency=currency,
            confirmation_url=confirmation.get("confirmation_url"),
            order_id=str(metadata["order_id"]) if metadata.get("order_id") else None,
            paid=bool(payload.get("paid", False)),
            created_at=created_at,
        )

    @staticmethod
    def _format_amount(amount_minor_units: int) -> str:
        return f"{amount_minor_units // 100}.{amount_minor_units % 100:02d}"

    @staticmethod
    def _parse_amount(value: str) -> int:
        try:
            decimal_value = Decimal(value)
        except InvalidOperation as exception:
            raise ValueError("invalid_currency_amount") from exception
        if not decimal_value.is_finite() or decimal_value < 0:
            raise ValueError("invalid_currency_amount")
        minor_units = decimal_value * 100
        if minor_units != minor_units.to_integral_value():
            raise ValueError("invalid_currency_precision")
        return int(minor_units)
