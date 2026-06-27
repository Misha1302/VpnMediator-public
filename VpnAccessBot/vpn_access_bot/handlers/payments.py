from __future__ import annotations

import asyncio
import hashlib
import logging

from aiogram import F, Router
from aiogram.types import Message, PreCheckoutQuery

from vpn_access_bot.advertising_readiness import CommerceOperationKind
from vpn_access_bot.config import Settings
from vpn_access_bot.constants import (
    ORDER_KIND_EXTEND,
    ORDER_KIND_EXTEND_AND_UPGRADE,
    ORDER_KIND_RESUME,
    ORDER_KIND_UPGRADE_DEVICES,
)
from vpn_access_bot.db import Database
from vpn_access_bot.mediator_client import MediatorClient
from vpn_access_bot.payment_processing import PaymentEvidence, PaymentInboxIngestionService
from vpn_access_bot.readiness import CommerceReadinessService
from vpn_access_bot.repositories import (
    NotificationOutboxRepository,
    OrderRepository,
    UserRepository,
)
from vpn_access_bot.services import PurchaseService

router = Router(name="payments")
logger = logging.getLogger(__name__)

_CHECKOUT_UNAVAILABLE_MESSAGE = (
    "Сейчас нельзя выдать новое подключение. Оплата временно отключена, "
    "чтобы деньги не списались без готового доступа."
)


def _operation_for_order_kind(order_kind: str) -> CommerceOperationKind:
    return {
        ORDER_KIND_EXTEND: CommerceOperationKind.RENEWAL,
        ORDER_KIND_RESUME: CommerceOperationKind.RESUME,
        ORDER_KIND_UPGRADE_DEVICES: CommerceOperationKind.UPGRADE_DEVICES,
        ORDER_KIND_EXTEND_AND_UPGRADE: CommerceOperationKind.EXTEND_AND_UPGRADE,
    }.get(order_kind, CommerceOperationKind.NEW_PURCHASE)


@router.pre_checkout_query()
async def handle_pre_checkout_query(
    query: PreCheckoutQuery,
    database: Database,
    settings: Settings,
    mediator_client: MediatorClient,
    readiness_service: CommerceReadinessService,
    bot_key: str | None = None,
) -> None:
    loop = asyncio.get_running_loop()
    started_at = loop.time()
    total_timeout = settings.pre_checkout_total_timeout_seconds
    validation_budget = total_timeout - settings.pre_checkout_answer_reserve_seconds
    is_valid = False
    error_message: str | None = _CHECKOUT_UNAVAILABLE_MESSAGE

    try:
        async with asyncio.timeout(validation_budget):
            # Prefer the recent shared readiness snapshot. A checkout must never block for the
            # Mediator's normal 15-second HTTP timeout because Telegram allows only 10 seconds.
            async with database.session() as session:
                order = await OrderRepository(session).get_for_payment_payload(
                    query.invoice_payload
                )
            operation_kind = (
                _operation_for_order_kind(order.order_kind)
                if order is not None
                else CommerceOperationKind.NEW_PURCHASE
            )
            readiness = await readiness_service.check(
                operation_kind=operation_kind,
                force=False,
                timeout_seconds=settings.pre_checkout_readiness_timeout_seconds,
            )
            if readiness.can_sell:
                async with database.session() as session:
                    service = PurchaseService(session, settings, mediator_client)
                    is_valid, error_message = await service.validate_order_before_checkout(
                        payload=query.invoice_payload,
                        amount_minor_units=query.total_amount,
                        currency=query.currency,
                        payer_telegram_id=query.from_user.id,
                        payment_bot_key=bot_key,
                    )
    except TimeoutError:
        logger.warning(
            "Pre-checkout validation exceeded its internal deadline: payer_telegram_id=%s",
            query.from_user.id,
        )
    except Exception:
        logger.exception(
            "Pre-checkout validation failed before Telegram acknowledgement: payer_telegram_id=%s",
            query.from_user.id,
        )

    remaining = max(total_timeout - (loop.time() - started_at), 0.1)
    try:
        async with asyncio.timeout(remaining):
            await query.answer(ok=is_valid, error_message=error_message)
    except TimeoutError:
        logger.error(
            "Telegram pre-checkout acknowledgement exceeded the total deadline: "
            "payer_telegram_id=%s",
            query.from_user.id,
        )


@router.message(F.successful_payment)
async def handle_successful_payment(
    message: Message,
    database: Database,
    bot_key: str | None = None,
) -> None:
    if message.from_user is None or message.successful_payment is None:
        return

    payment = message.successful_payment
    async with database.session() as session:
        inbox = await PaymentInboxIngestionService(session).ingest_telegram_stars(
            PaymentEvidence(
                invoice_payload=payment.invoice_payload,
                amount_minor_units=payment.total_amount,
                currency=payment.currency,
                provider_charge_id=payment.telegram_payment_charge_id,
                payer_telegram_id=message.from_user.id,
                provider_occurred_at_utc=message.date,
                payment_bot_key=bot_key,
            )
        )
        user = await UserRepository(session).get_or_create_from_message_user(
            telegram_id=message.from_user.id,
            username=message.from_user.username,
            first_name=message.from_user.first_name,
        )
        charge_fingerprint = hashlib.sha256(
            payment.telegram_payment_charge_id.encode("utf-8")
        ).hexdigest()
        notification_kind = (
            "payment_manual_review"
            if inbox.reconciliation_status == "manual_review"
            else "payment_received"
        )
        await NotificationOutboxRepository(session).enqueue_once(
            idempotency_key=f"payment-confirmation:{charge_fingerprint}",
            notification_kind=notification_kind,
            user_id=user.id,
            bot_key=bot_key,
        )
