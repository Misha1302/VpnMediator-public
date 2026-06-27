from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from sqlalchemy.ext.asyncio import AsyncSession

from vpn_access_bot.constants import (
    PAYMENT_MODE_TELEGRAM_STARS,
    PAYMENT_MODE_YOOKASSA_SBP,
)
from vpn_access_bot.models import PaymentInbox
from vpn_access_bot.repositories import PaymentInboxRepository


@dataclass(frozen=True)
class PaymentEvidence:
    invoice_payload: str
    amount_minor_units: int
    currency: str
    provider_charge_id: str
    payer_telegram_id: int
    provider_occurred_at_utc: datetime | None = None
    payment_bot_key: str | None = None
    origin_bot_key: str | None = None


class PaymentInboxIngestionService:
    """Persists provider evidence without executing order or entitlement logic."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def ingest_telegram_stars(self, evidence: PaymentEvidence) -> PaymentInbox:
        inbox, _ = await PaymentInboxRepository(self._session).receive(
            provider=PAYMENT_MODE_TELEGRAM_STARS,
            provider_charge_id=evidence.provider_charge_id,
            invoice_payload=evidence.invoice_payload,
            payer_external_id=str(evidence.payer_telegram_id),
            amount_minor_units=evidence.amount_minor_units,
            currency=evidence.currency,
            provider_occurred_at_utc=evidence.provider_occurred_at_utc,
            payment_bot_key=evidence.payment_bot_key or evidence.origin_bot_key,
        )
        return inbox

    async def ingest_yookassa_sbp(self, evidence: PaymentEvidence) -> PaymentInbox:
        inbox, _ = await PaymentInboxRepository(self._session).receive(
            provider=PAYMENT_MODE_YOOKASSA_SBP,
            provider_charge_id=evidence.provider_charge_id,
            invoice_payload=evidence.invoice_payload,
            payer_external_id=str(evidence.payer_telegram_id),
            amount_minor_units=evidence.amount_minor_units,
            currency=evidence.currency,
            provider_occurred_at_utc=evidence.provider_occurred_at_utc,
            payment_bot_key=evidence.payment_bot_key or evidence.origin_bot_key,
        )
        return inbox
