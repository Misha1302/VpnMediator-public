from __future__ import annotations

import hashlib
import html
import json
import logging
import re
from datetime import UTC, datetime
from urllib.parse import quote, urlparse

from aiohttp import web

from vpn_access_bot.advertising_readiness import CommerceOperationKind
from vpn_access_bot.checkout_tokens import CheckoutTokenCodec, CheckoutTokenError
from vpn_access_bot.commerce import PricingService
from vpn_access_bot.config import Settings
from vpn_access_bot.constants import (
    ORDER_KIND_EXTEND,
    ORDER_KIND_EXTEND_AND_UPGRADE,
    ORDER_KIND_RESUME,
    ORDER_KIND_UPGRADE_DEVICES,
    ORDER_STATUS_ACTIVATING,
    ORDER_STATUS_PAID,
    ORDER_STATUS_PAYMENT_RECEIVED,
    PAYMENT_MODE_YOOKASSA_SBP,
)
from vpn_access_bot.mediator_client import MediatorClient
from vpn_access_bot.models import utc_now
from vpn_access_bot.payment_processing import PaymentEvidence, PaymentInboxIngestionService
from vpn_access_bot.readiness import CommerceReadinessService
from vpn_access_bot.repositories import (
    DiscountRedemptionRepository,
    OrderRepository,
    PaymentInboxRepository,
    PurchaseQuoteRepository,
    to_aware_utc,
)
from vpn_access_bot.services import PurchaseService
from vpn_access_bot.yookassa import YooKassaClient, YooKassaError, YooKassaPayment

logger = logging.getLogger(__name__)
YOOKASSA_PAYMENT_ID_PATTERN = re.compile(
    r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-"
    r"[0-9a-fA-F]{4}-[0-9a-fA-F]{12}"
)


class CheckoutWebServer:
    def __init__(
        self,
        *,
        settings: Settings,
        database,
        mediator_client: MediatorClient,
        readiness: CommerceReadinessService,
        yookassa: YooKassaClient,
    ) -> None:
        self._settings = settings
        self._database = database
        self._mediator_client = mediator_client
        self._readiness = readiness
        self._yookassa = yookassa
        self._tokens = CheckoutTokenCodec(settings.checkout_token_secret.get_secret_value())
        self._runner: web.AppRunner | None = None

    async def start(self) -> None:
        app = web.Application(client_max_size=32 * 1024)
        app.router.add_get("/checkout/{token}", self._checkout)
        app.router.add_post("/checkout/{token}/pay", self._pay)
        app.router.add_get("/payment/return", self._return)
        app.router.add_get("/payment/{token}/status", self._status)
        webhook_secret = self._settings.yookassa_webhook_path_secret.get_secret_value()
        app.router.add_post(f"/webhooks/yookassa/{webhook_secret}", self._webhook)
        self._runner = web.AppRunner(app, access_log=None)
        await self._runner.setup()
        site = web.TCPSite(
            self._runner,
            host=self._settings.checkout_bind_host,
            port=self._settings.checkout_bind_port,
        )
        await site.start()

    async def close(self) -> None:
        if self._runner is not None:
            await self._runner.cleanup()
            self._runner = None

    async def _checkout(self, request: web.Request) -> web.Response:
        token = request.match_info["token"]
        try:
            claims = self._tokens.verify(token)
        except CheckoutTokenError as exception:
            return self._page("Ссылка недействительна", self._error_text(str(exception)), 410)

        async with self._database.session() as session:
            quote_record = await PurchaseQuoteRepository(session).get_by_public_id(claims.quote_id)
            if quote_record is None:
                return self._page("Заказ не найден", "Создайте новый расчёт в Telegram-боте.", 404)
            order = await OrderRepository(session).get_for_quote(quote_record.id)
            offer = PricingService(self._settings).calculate_quote_offer(
                quote_record, PAYMENT_MODE_YOOKASSA_SBP
            )

        if order is None and to_aware_utc(quote_record.expires_at_utc) <= utc_now():
            return self._page(
                "Расчёт устарел",
                "Создайте новый расчёт в Telegram-боте перед оплатой.",
                410,
            )

        expected_amount = (
            order.amount_minor_units if order is not None else offer.amount_minor_units
        )
        expected_pricing = order.pricing_version if order is not None else offer.pricing_version
        if not self._claims_match(claims, expected_amount, expected_pricing):
            return self._page(
                "Цена изменилась",
                "Создайте новый расчёт в Telegram-боте перед оплатой.",
                409,
            )

        if order is not None and order.provider != PAYMENT_MODE_YOOKASSA_SBP:
            return self._page(
                "Выбран другой способ оплаты",
                "Для этого расчёта уже выбрана оплата звёздами.",
                409,
            )
        if order is not None and order.status in {
            ORDER_STATUS_PAYMENT_RECEIVED,
            ORDER_STATUS_ACTIVATING,
            ORDER_STATUS_PAID,
        }:
            return self._success_page()
        if order is not None and order.provider_payment_status == "canceled":
            return self._page(
                "Платёж отменён",
                "Деньги не списаны. Создайте новый расчёт в Telegram-боте.",
                409,
            )
        if order is not None and order.provider_payment_id is not None:
            return self._page(
                "Проверяем оплату",
                '<p class="lead">Подтверждение от банка ещё не получено.</p>'
                f'<a class="button" href="/checkout/{html.escape(token)}">Проверить снова</a>',
                202,
            )

        amount = self._format_rub(offer.amount_minor_units)
        content = f"""
            <p class="lead">Оплата доступа к Razaltush VPN</p>
            <dl>
              <div><dt>Срок</dt><dd>{quote_record.duration_days} дней</dd></div>
              <div><dt>Устройства</dt><dd>{quote_record.max_devices}</dd></div>
              <div><dt>К оплате</dt><dd>{amount}</dd></div>
            </dl>
            <p class="notice">Разовый платёж. Автопродления нет.</p>
            <form method="post" action="/checkout/{html.escape(token)}/pay">
              <button type="submit">Оплатить по СБП</button>
            </form>
            <p class="muted">После оплаты доступ появится в Telegram-боте автоматически.</p>
        """
        return self._page("Оплата по СБП", content)

    async def _pay(self, request: web.Request) -> web.StreamResponse:
        token = request.match_info["token"]
        try:
            claims = self._tokens.verify(token)
        except CheckoutTokenError as exception:
            return self._page("Ссылка недействительна", self._error_text(str(exception)), 410)

        async with self._database.session() as session:
            quote_record = await PurchaseQuoteRepository(session).get_by_public_id(claims.quote_id)
            if quote_record is None:
                return self._page("Заказ не найден", "Создайте новый расчёт в Telegram-боте.", 404)
            operation = self._operation_for_order_kind(quote_record.order_kind)
            existing_order = await OrderRepository(session).get_for_quote(quote_record.id)
            offer = PricingService(self._settings).calculate_quote_offer(
                quote_record, PAYMENT_MODE_YOOKASSA_SBP
            )
            expected_amount = (
                existing_order.amount_minor_units
                if existing_order is not None
                else offer.amount_minor_units
            )
            expected_pricing = (
                existing_order.pricing_version
                if existing_order is not None
                else offer.pricing_version
            )
            if not self._claims_match(claims, expected_amount, expected_pricing):
                return self._page(
                    "Цена изменилась",
                    "Создайте новый расчёт в Telegram-боте перед оплатой.",
                    409,
                )

        readiness = await self._readiness.check(operation_kind=operation, force=True)
        if not readiness.can_sell:
            return self._page(
                "Оплата временно недоступна",
                "Мы не принимаем деньги, пока не можем гарантировать выдачу доступа.",
                503,
            )

        try:
            async with self._database.session() as session:
                quote_record = await PurchaseQuoteRepository(session).get_by_public_id(
                    claims.quote_id
                )
                if quote_record is None:
                    raise ValueError("quote_not_found")
                order = await PurchaseService(
                    session, self._settings, self._mediator_client
                ).create_order_from_quote(
                    quote_record.public_quote_id,
                    quote_record.user.telegram_id,
                    payment_provider=PAYMENT_MODE_YOOKASSA_SBP,
                )
                if not self._claims_match(claims, order.amount_minor_units, order.pricing_version):
                    raise ValueError("checkout_offer_changed")
                authorized_at = utc_now()
                authorized_until = order.expires_at_utc or authorized_at
                if not await OrderRepository(session).try_mark_checkout_authorized(
                    order,
                    authorized_at_utc=authorized_at,
                    authorized_until_utc=authorized_until,
                ):
                    raise ValueError("order_checkout_unavailable")
        except ValueError as exception:
            return self._page("Заказ уже недоступен", html.escape(str(exception)), 409)

        if order.provider_confirmation_url:
            try:
                return self._redirect_to_provider(order.provider_confirmation_url)
            except YooKassaError:
                return self._page(
                    "Платёж временно недоступен",
                    "Сохранённая ссылка провайдера отклонена проверкой безопасности.",
                    502,
                )

        idempotence_key = hashlib.sha256(
            f"yookassa:create:{order.public_order_id}".encode()
        ).hexdigest()
        return_url = f"{self._settings.yookassa_return_url}?checkout={quote(token)}"
        try:
            payment = await self._yookassa.create_sbp_payment(
                order_id=order.public_order_id,
                amount_minor_units=order.amount_minor_units,
                return_url=return_url,
                idempotence_key=idempotence_key,
                description=f"Razaltush VPN, заказ {order.public_order_id[:8]}",
            )
            self._validate_payment(payment, order.public_order_id, order.amount_minor_units)
            if payment.confirmation_url is not None:
                self._validate_confirmation_url(payment.confirmation_url)
        except (YooKassaError, ValueError):
            logger.exception("YooKassa payment creation failed: order_id=%s", order.public_order_id)
            return self._page(
                "Платёж не создан",
                "Деньги не списаны. Повторите попытку через несколько минут.",
                502,
            )

        async with self._database.session() as session:
            persisted = await OrderRepository(session).get_by_public_id(order.public_order_id)
            if persisted is None:
                raise RuntimeError("order_disappeared_after_provider_creation")
            await OrderRepository(session).attach_provider_payment(
                persisted,
                provider_payment_id=payment.payment_id,
                provider_payment_status=payment.status,
                confirmation_url=payment.confirmation_url,
            )

        if payment.status == "succeeded" and payment.paid:
            persisted = await self._persist_succeeded_payment(payment, order.public_order_id)
            return (
                self._success_page()
                if persisted
                else self._page(
                    "Платёж требует проверки",
                    "Оплата сохранена. Поддержка проверит её, повторно платить не нужно.",
                    202,
                )
            )
        if payment.confirmation_url is None:
            return self._page(
                "Платёж ожидает подтверждения",
                "Обновите страницу через несколько секунд.",
                202,
            )
        return self._redirect_to_provider(payment.confirmation_url)

    async def _return(self, request: web.Request) -> web.StreamResponse:
        token = request.query.get("checkout", "")
        try:
            self._tokens.verify(token)
        except CheckoutTokenError as exception:
            return self._page("Ссылка недействительна", self._error_text(str(exception)), 410)
        raise web.HTTPSeeOther(location=f"/checkout/{quote(token)}")

    async def _status(self, request: web.Request) -> web.Response:
        token = request.match_info["token"]
        try:
            claims = self._tokens.verify(token)
        except CheckoutTokenError:
            return web.json_response({"status": "expired"}, status=410)
        async with self._database.session() as session:
            quote_record = await PurchaseQuoteRepository(session).get_by_public_id(claims.quote_id)
            order = (
                await OrderRepository(session).get_for_quote(quote_record.id)
                if quote_record is not None
                else None
            )
        return web.json_response(
            {
                "status": order.status if order is not None else "not_started",
                "providerStatus": order.provider_payment_status if order is not None else None,
            },
            headers={"Cache-Control": "no-store"},
        )

    async def _webhook(self, request: web.Request) -> web.Response:
        try:
            payload = json.loads(await request.text())
            event = str(payload["event"])
            payment_id = str(payload["object"]["id"])
        except (KeyError, TypeError, ValueError, json.JSONDecodeError):
            return web.json_response({"status": "invalid"}, status=400)
        if YOOKASSA_PAYMENT_ID_PATTERN.fullmatch(payment_id) is None:
            return web.json_response({"status": "invalid"}, status=400)
        if event not in {"payment.succeeded", "payment.canceled"}:
            return web.json_response({"status": "ignored"})

        try:
            payment = await self._yookassa.get_payment(payment_id)
        except YooKassaError:
            logger.exception("YooKassa webhook verification failed: payment_id=%s", payment_id)
            return web.json_response({"status": "verification_failed"}, status=503)

        async with self._database.session() as session:
            repository = OrderRepository(session)
            order = await repository.get_by_provider_payment_id(
                PAYMENT_MODE_YOOKASSA_SBP, payment.payment_id
            )
            if order is None and payment.order_id is not None:
                order = await repository.get_by_public_id(payment.order_id)
                if order is not None and order.provider == PAYMENT_MODE_YOOKASSA_SBP:
                    await repository.attach_provider_payment(
                        order,
                        provider_payment_id=payment.payment_id,
                        provider_payment_status=payment.status,
                        confirmation_url=payment.confirmation_url,
                    )
            if order is None:
                logger.error("Verified YooKassa payment has no order: payment_id=%s", payment_id)
                return web.json_response({"status": "unknown_order"}, status=503)
            await repository.update_provider_payment_status(order, payment.status)
            if payment.status == "canceled":
                await DiscountRedemptionRepository(session).release_for_order(order.id)
            public_order_id = order.public_order_id

        verified_action = self._verified_payment_action(payment)
        if verified_action == "canceled":
            return web.json_response({"status": "accepted"})
        if verified_action == "ignored":
            return web.json_response({"status": "ignored"})
        persisted = await self._persist_succeeded_payment(payment, public_order_id)
        if not persisted:
            logger.error("YooKassa payment evidence mismatch: payment_id=%s", payment_id)
            return web.json_response({"status": "manual_review"})
        return web.json_response({"status": "accepted"})

    @staticmethod
    def _verified_payment_action(payment: YooKassaPayment) -> str:
        if payment.status == "canceled":
            return "canceled"
        if payment.status == "succeeded" and payment.paid:
            return "succeeded"
        return "ignored"

    async def _persist_succeeded_payment(
        self, payment: YooKassaPayment, public_order_id: str
    ) -> bool:
        async with self._database.session() as session:
            order = await OrderRepository(session).get_by_public_id(public_order_id)
            if order is None:
                raise ValueError("order_not_found")
            inbox = await PaymentInboxIngestionService(session).ingest_yookassa_sbp(
                PaymentEvidence(
                    invoice_payload=order.invoice_payload,
                    amount_minor_units=payment.amount_minor_units,
                    currency=payment.currency,
                    provider_charge_id=payment.payment_id,
                    payer_telegram_id=order.user.telegram_id,
                    provider_occurred_at_utc=payment.created_at or datetime.now(UTC),
                    origin_bot_key=order.origin_bot_key,
                )
            )
            evidence_valid = (
                payment.status == "succeeded"
                and payment.paid
                and payment.order_id == order.public_order_id
                and payment.amount_minor_units == order.amount_minor_units
                and payment.currency == "RUB"
            )
            if not evidence_valid:
                await PaymentInboxRepository(session).mark_manual_review(
                    inbox, "verified_provider_evidence_mismatch"
                )
            return evidence_valid

    @staticmethod
    def _validate_payment(
        payment: YooKassaPayment, expected_order_id: str, expected_amount: int
    ) -> None:
        if (
            payment.order_id != expected_order_id
            or payment.amount_minor_units != expected_amount
            or payment.currency != "RUB"
        ):
            raise ValueError("payment_evidence_mismatch")

    @staticmethod
    def _operation_for_order_kind(order_kind: str) -> CommerceOperationKind:
        return {
            ORDER_KIND_EXTEND: CommerceOperationKind.RENEWAL,
            ORDER_KIND_RESUME: CommerceOperationKind.RESUME,
            ORDER_KIND_UPGRADE_DEVICES: CommerceOperationKind.UPGRADE_DEVICES,
            ORDER_KIND_EXTEND_AND_UPGRADE: CommerceOperationKind.EXTEND_AND_UPGRADE,
        }.get(order_kind, CommerceOperationKind.NEW_PURCHASE)

    @staticmethod
    def _claims_match(claims, amount_minor_units: int, pricing_version: str) -> bool:
        return (
            claims.amount_minor_units == amount_minor_units
            and claims.pricing_version == pricing_version
        )

    @staticmethod
    def _redirect_to_provider(url: str) -> web.StreamResponse:
        CheckoutWebServer._validate_confirmation_url(url)
        raise web.HTTPSeeOther(location=url)

    @staticmethod
    def _validate_confirmation_url(url: str) -> None:
        parsed = urlparse(url)
        if parsed.scheme != "https" or not parsed.netloc or "\r" in url or "\n" in url:
            raise YooKassaError("yookassa_confirmation_url_invalid")

    def _success_page(self) -> web.Response:
        username = self._settings.public_telegram_bot_username.lstrip("@")
        return self._page(
            "Оплата получена",
            f'<p class="lead">Доступ активируется автоматически.</p>'
            f'<a class="button" href="https://t.me/{html.escape(username)}">'
            "Открыть Telegram-бота</a>",
        )

    @staticmethod
    def _format_rub(minor_units: int) -> str:
        return f"{minor_units // 100} ₽" if minor_units % 100 == 0 else f"{minor_units / 100:.2f} ₽"

    @staticmethod
    def _error_text(error_code: str) -> str:
        return (
            "Срок ссылки закончился. Создайте новый расчёт в Telegram-боте."
            if error_code == "checkout_token_expired"
            else "Ссылка повреждена или больше не действует."
        )

    @staticmethod
    def _page(title: str, content: str, status: int = 200) -> web.Response:
        document = f"""<!doctype html>
<html lang="ru">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>{html.escape(title)} — Razaltush VPN</title>
<style>
* {{ box-sizing: border-box; }}
body {{ margin: 0; background: #f4f7f7; color: #17212b; font-family: system-ui,sans-serif; }}
main {{ width: min(560px,100%); margin: 0 auto; padding: 48px 20px; }}
h1 {{ font-size: 2rem; margin: 0 0 20px; }}
.lead {{ font-size: 1.1rem; line-height: 1.55; }}
dl {{ border-top: 1px solid #cad6d6; border-bottom: 1px solid #cad6d6; }}
dl div {{ display: flex; justify-content: space-between; gap: 16px; padding: 14px 0; }}
dt {{ color: #52606d; }}
dd {{ margin: 0; font-weight: 700; }}
.notice {{ padding: 12px; border-left: 4px solid #0f766e; background: #e7f6f3; }}
button,.button {{ display: flex; width: 100%; min-height: 52px; align-items: center;
justify-content: center; border: 0; border-radius: 8px; background: #0f766e; color: white;
font-size: 1rem; font-weight: 700; text-decoration: none; cursor: pointer; }}
button:hover,.button:hover {{ background: #0b665f; }}
.muted {{ color: #52606d; font-size: .92rem; line-height: 1.5; }}
</style>
</head>
<body><main><p class="muted">Razaltush VPN</p><h1>{html.escape(title)}</h1>
{content}</main></body></html>"""
        return web.Response(
            text=document,
            status=status,
            content_type="text/html",
            charset="utf-8",
            headers={
                "Cache-Control": "no-store",
                "Content-Security-Policy": (
                    "default-src 'none'; style-src 'unsafe-inline'; form-action 'self'; "
                    "base-uri 'none'; frame-ancestors 'none'"
                ),
                "Referrer-Policy": "no-referrer",
                "X-Content-Type-Options": "nosniff",
                "X-Frame-Options": "DENY",
            },
        )
