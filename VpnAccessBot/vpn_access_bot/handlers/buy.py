from __future__ import annotations

from datetime import timedelta

from aiogram import F, Router
from aiogram.types import CallbackQuery, LabeledPrice

from vpn_access_bot.advertising_readiness import CommerceOperationKind
from vpn_access_bot.checkout_tokens import CheckoutTokenCodec
from vpn_access_bot.commerce import PricingService
from vpn_access_bot.config import Settings
from vpn_access_bot.constants import (
    ORDER_KIND_EXTEND,
    ORDER_KIND_EXTEND_AND_UPGRADE,
    ORDER_KIND_PURCHASE,
    ORDER_KIND_RESUME,
    ORDER_KIND_UPGRADE_DEVICES,
    ORDER_STATUS_PENDING,
    PAYMENT_MODE_MANUAL,
    PAYMENT_MODE_TELEGRAM_STARS,
    PAYMENT_MODE_YOOKASSA_SBP,
)
from vpn_access_bot.db import Database
from vpn_access_bot.expiration import calculate_expiration_snapshot
from vpn_access_bot.formatting import (
    days_ru,
    devices_ru,
    format_access_through_date_ru,
)
from vpn_access_bot.keyboards import (
    after_purchase_keyboard,
    back_to_main_keyboard,
    continue_sbp_keyboard,
    format_price,
    purchase_other_devices_keyboard,
    purchase_other_packages_keyboard,
    purchase_packages_keyboard,
    purchase_periods_keyboard,
    quote_confirmation_keyboard,
    service_unavailable_keyboard,
    upgrade_devices_keyboard,
    upgrade_other_devices_keyboard,
)
from vpn_access_bot.mediator_client import MediatorClient
from vpn_access_bot.models import Order, PurchaseQuote, utc_now
from vpn_access_bot.product_catalog import ProductCatalog
from vpn_access_bot.readiness import CommerceReadinessService
from vpn_access_bot.repositories import (
    OrderRepository,
    ProductEventRepository,
    PurchaseQuoteRepository,
    SubscriptionRepository,
    UserRepository,
    to_aware_utc,
)
from vpn_access_bot.services import PurchaseService, TelegramStarsInvoice
from vpn_access_bot.telegram import BotRegistry
from vpn_access_bot.texts import format_datetime
from vpn_access_bot.user_texts import commerce_unavailable_text

router = Router(name="buy")


def _build_invoice_if_required(
    service: PurchaseService,
    order: Order,
    settings: Settings,
) -> TelegramStarsInvoice | None:
    if order.provider != PAYMENT_MODE_TELEGRAM_STARS or order.amount_minor_units <= 0:
        return None

    return service.build_telegram_stars_invoice(order)


def _commerce_operation_for_order_kind(order_kind: str) -> CommerceOperationKind:
    return {
        ORDER_KIND_EXTEND: CommerceOperationKind.RENEWAL,
        ORDER_KIND_RESUME: CommerceOperationKind.RESUME,
        ORDER_KIND_UPGRADE_DEVICES: CommerceOperationKind.UPGRADE_DEVICES,
        ORDER_KIND_EXTEND_AND_UPGRADE: CommerceOperationKind.EXTEND_AND_UPGRADE,
    }.get(order_kind, CommerceOperationKind.NEW_PURCHASE)


async def _check_readiness(
    callback: CallbackQuery,
    readiness_service: CommerceReadinessService,
    *,
    operation_kind: CommerceOperationKind = CommerceOperationKind.NEW_PURCHASE,
    force: bool = False,
) -> bool:
    readiness = await readiness_service.check(
        operation_kind=operation_kind,
        force=force,
    )
    if readiness.can_sell:
        return True
    if callback.message is not None:
        await callback.message.edit_text(
            commerce_unavailable_text(),
            reply_markup=service_unavailable_keyboard(),
        )
    return False


async def _get_primary_subscription(database: Database, telegram_id: int):
    async with database.session() as session:
        user = await UserRepository(session).get_by_telegram_id(telegram_id)
        if user is None:
            return None
        return await SubscriptionRepository(session).get_primary_for_user(user)


async def _record_quote_shown(database: Database, quote: PurchaseQuote) -> None:
    async with database.session() as session:
        await ProductEventRepository(session).record(
            event_name="quote_shown",
            user_id=quote.user_id,
            idempotency_key=f"quote_shown:{quote.public_quote_id}",
            payload={
                "order_kind": quote.order_kind,
                "period_count": quote.period_count,
                "max_devices": quote.max_devices,
            },
        )


@router.callback_query(F.data == "buy:menu")
async def handle_buy_menu(
    callback: CallbackQuery,
    settings: Settings,
    readiness_service: CommerceReadinessService,
) -> None:
    await callback.answer()
    if callback.message is None or not await _check_readiness(
        callback,
        readiness_service,
        force=True,
    ):
        return
    catalog = ProductCatalog.from_settings(settings)
    await callback.message.edit_text(
        "<b>Выберите количество устройств и срок</b>\n\n"
        "Формат кнопки: <b>устройства × дни · цена</b>. Каждая кнопка сразу "
        "задаёт оба параметра. Телефон и компьютер считаются как два устройства. "
        "Скидка за срок уже учтена, а личная скидка применится в итоговом расчёте.",
        reply_markup=purchase_packages_keyboard(catalog),
    )


@router.callback_query(F.data.in_({"buy:renew", "buy:resume"}))
async def handle_period_operation_menu(
    callback: CallbackQuery,
    database: Database,
    settings: Settings,
    readiness_service: CommerceReadinessService,
) -> None:
    await callback.answer()
    if callback.from_user is None or callback.message is None:
        return
    requested_operation = (
        CommerceOperationKind.RENEWAL
        if callback.data == "buy:renew"
        else CommerceOperationKind.RESUME
    )
    if not await _check_readiness(callback, readiness_service, operation_kind=requested_operation):
        return
    subscription = await _get_primary_subscription(database, callback.from_user.id)
    if subscription is None:
        await callback.message.edit_text(
            "Подписка не найдена. Откройте главное меню и выберите покупку.",
            reply_markup=back_to_main_keyboard(),
        )
        return
    operation_kind = ORDER_KIND_EXTEND if callback.data == "buy:renew" else ORDER_KIND_RESUME
    title = (
        "На сколько дней продлить доступ?"
        if operation_kind == ORDER_KIND_EXTEND
        else "На сколько дней возобновить доступ?"
    )
    catalog = ProductCatalog.from_settings(settings)
    await callback.message.edit_text(
        f"<b>{title}</b>\n\n"
        f"Текущий лимит сохранится: <b>{devices_ru(subscription.max_devices)}</b>.\n"
        "Цена указана по текущему каталогу; личная скидка применится "
        "в итоговом расчёте.",
        reply_markup=purchase_periods_keyboard(
            catalog,
            selected_devices=None,
            price_devices=subscription.max_devices,
            operation_kind=operation_kind,
            grandfathered_device_limit=subscription.max_devices,
        ),
    )


@router.callback_query(F.data == "buy:upgrade")
async def handle_upgrade_menu(
    callback: CallbackQuery,
    database: Database,
    settings: Settings,
    readiness_service: CommerceReadinessService,
) -> None:
    await callback.answer()
    if callback.from_user is None or callback.message is None:
        return
    if not await _check_readiness(
        callback,
        readiness_service,
        operation_kind=CommerceOperationKind.UPGRADE_DEVICES,
    ):
        return
    subscription = await _get_primary_subscription(database, callback.from_user.id)
    if subscription is None:
        await callback.message.edit_text(
            "Сначала активируйте или купите доступ.",
            reply_markup=back_to_main_keyboard(),
        )
        return
    catalog = ProductCatalog.from_settings(settings)
    await callback.message.edit_text(
        "<b>До скольких устройств увеличить лимит?</b>\n\n"
        f"Сейчас: <b>{devices_ru(subscription.max_devices)}</b>.",
        reply_markup=upgrade_devices_keyboard(
            subscription.max_devices,
            catalog.device_options,
        ),
    )


@router.callback_query(F.data == "buy:upgrade_other")
async def handle_upgrade_other_device_options(
    callback: CallbackQuery,
    database: Database,
    settings: Settings,
    readiness_service: CommerceReadinessService,
) -> None:
    await callback.answer()
    if callback.from_user is None or callback.message is None:
        return
    if not await _check_readiness(
        callback,
        readiness_service,
        operation_kind=CommerceOperationKind.UPGRADE_DEVICES,
    ):
        return
    subscription = await _get_primary_subscription(database, callback.from_user.id)
    if subscription is None:
        await callback.message.edit_text(
            "Сначала активируйте или купите доступ.",
            reply_markup=back_to_main_keyboard(),
        )
        return
    catalog = ProductCatalog.from_settings(settings)
    await callback.message.edit_text(
        "<b>Выберите точное количество устройств</b>",
        reply_markup=upgrade_other_devices_keyboard(
            subscription.max_devices,
            catalog.device_options,
        ),
    )


@router.callback_query(F.data.startswith("buy:upgrade_devices:"))
async def handle_upgrade_devices_selected(
    callback: CallbackQuery,
    database: Database,
    settings: Settings,
    mediator_client: MediatorClient,
    readiness_service: CommerceReadinessService,
) -> None:
    await callback.answer()
    if callback.from_user is None or callback.message is None or callback.data is None:
        return
    if not await _check_readiness(
        callback,
        readiness_service,
        operation_kind=CommerceOperationKind.UPGRADE_DEVICES,
    ):
        return
    try:
        max_devices = int(callback.data.rsplit(":", maxsplit=1)[-1])
        async with database.session() as session:
            quote = await PurchaseService(session, settings, mediator_client).create_quote(
                telegram_id=callback.from_user.id,
                username=callback.from_user.username,
                first_name=callback.from_user.first_name,
                period_count=0,
                max_devices=max_devices,
                order_kind=ORDER_KIND_UPGRADE_DEVICES,
            )
    except (TypeError, ValueError):
        await callback.message.edit_text(
            "Нельзя увеличить лимит до этого значения. Проверьте состояние VPN и попробуйте снова.",
            reply_markup=back_to_main_keyboard(),
        )
        return
    price_text = (
        "Бесплатно"
        if quote.amount_minor_units == 0
        else format_price(quote.amount_minor_units, quote.currency)
    )
    await callback.message.edit_text(
        "<b>Увеличение числа устройств</b>\n\n"
        f"Новый лимит: <b>{devices_ru(quote.max_devices)}</b>\n"
        f"Стоимость до конца текущего срока: <b>{price_text}</b>",
        reply_markup=quote_confirmation_keyboard(
            quote.public_quote_id,
            edit_callback="buy:upgrade",
            complimentary=quote.amount_minor_units == 0,
        ),
    )
    await _record_quote_shown(database, quote)


@router.callback_query(F.data == "buy:devices_other")
async def handle_other_device_options(
    callback: CallbackQuery,
    settings: Settings,
) -> None:
    await callback.answer()
    if callback.message is None:
        return
    catalog = ProductCatalog.from_settings(settings)
    await callback.message.edit_text(
        "<b>Выберите точное количество устройств</b>\n\n"
        "Телефон, компьютер, планшет и телевизор считаются отдельными устройствами.",
        reply_markup=purchase_other_devices_keyboard(catalog.device_options),
    )


@router.callback_query(F.data == "buy:packages_other")
async def handle_other_package_options(
    callback: CallbackQuery,
    settings: Settings,
    readiness_service: CommerceReadinessService,
) -> None:
    await callback.answer()
    if callback.message is None or not await _check_readiness(
        callback,
        readiness_service,
        force=True,
    ):
        return
    catalog = ProductCatalog.from_settings(settings)
    await callback.message.edit_text(
        "<b>Другие варианты количества устройств и срока</b>\n\n"
        "Формат кнопки: <b>устройства × дни · цена</b>. Каждая кнопка сразу "
        "задаёт оба параметра. Скидка за срок уже учтена, цена указана до личной скидки.",
        reply_markup=purchase_other_packages_keyboard(catalog),
    )


@router.callback_query(F.data.startswith("buy:devices:"))
async def handle_devices_selected(
    callback: CallbackQuery,
    database: Database,
    settings: Settings,
) -> None:
    await callback.answer()
    if callback.from_user is None or callback.message is None or callback.data is None:
        return
    try:
        devices = int(callback.data.rsplit(":", maxsplit=1)[-1])
        catalog = ProductCatalog.from_settings(settings)
        catalog.validate_device_limit(devices)
    except (TypeError, ValueError):
        await callback.message.edit_text(
            "Такое количество устройств недоступно.",
            reply_markup=back_to_main_keyboard(),
        )
        return
    async with database.session() as session:
        user = await UserRepository(session).get_by_telegram_id(callback.from_user.id)
        if user is not None:
            await ProductEventRepository(session).record(
                event_name="devices_selected",
                user_id=user.id,
                idempotency_key=f"devices_selected:{callback.id}",
                payload={"max_devices": devices, "order_kind": ORDER_KIND_PURCHASE},
            )
    await callback.message.edit_text(
        "<b>Шаг 2 из 2. На сколько дней?</b>\n\n"
        "Цена указана для выбранного числа устройств. "
        "Личная скидка применится в итоговом расчёте.",
        reply_markup=purchase_periods_keyboard(
            catalog,
            selected_devices=devices,
            price_devices=devices,
            operation_kind=ORDER_KIND_PURCHASE,
        ),
    )


@router.callback_query(F.data.startswith("buy:period:"))
async def handle_purchase_period_selected(
    callback: CallbackQuery,
    database: Database,
    settings: Settings,
    mediator_client: MediatorClient,
    readiness_service: CommerceReadinessService,
) -> None:
    await callback.answer()
    if callback.from_user is None or callback.message is None or callback.data is None:
        return
    if not await _check_readiness(callback, readiness_service):
        return
    try:
        _, _, devices_text, period_text = callback.data.split(":", maxsplit=3)
        devices = int(devices_text)
        period_count = int(period_text)
        async with database.session() as session:
            quote = await PurchaseService(session, settings, mediator_client).create_quote(
                telegram_id=callback.from_user.id,
                username=callback.from_user.username,
                first_name=callback.from_user.first_name,
                period_count=period_count,
                max_devices=devices,
                order_kind=ORDER_KIND_PURCHASE,
            )
    except (TypeError, ValueError):
        await callback.message.edit_text(
            "Состояние подписки или условия покупки изменились. "
            "Откройте главное меню и попробуйте снова.",
            reply_markup=back_to_main_keyboard(),
        )
        return
    await _show_quote(
        callback,
        quote,
        database=database,
        settings=settings,
        edit_callback="buy:menu",
    )


@router.callback_query(F.data.regexp(r"^buy:(extend|resume):period:\d+$"))
async def handle_existing_subscription_period_selected(
    callback: CallbackQuery,
    database: Database,
    settings: Settings,
    mediator_client: MediatorClient,
    readiness_service: CommerceReadinessService,
) -> None:
    await callback.answer()
    if callback.from_user is None or callback.message is None or callback.data is None:
        return
    _, operation_kind, _, period_text = callback.data.split(":", maxsplit=3)
    if not await _check_readiness(
        callback,
        readiness_service,
        operation_kind=_commerce_operation_for_order_kind(operation_kind),
    ):
        return
    try:
        period_count = int(period_text)
        subscription = await _get_primary_subscription(database, callback.from_user.id)
        if subscription is None:
            raise ValueError("subscription_required")
        async with database.session() as session:
            quote = await PurchaseService(session, settings, mediator_client).create_quote(
                telegram_id=callback.from_user.id,
                username=callback.from_user.username,
                first_name=callback.from_user.first_name,
                period_count=period_count,
                max_devices=subscription.max_devices,
                order_kind=operation_kind,
                target_subscription_id=subscription.id,
            )
    except (TypeError, ValueError):
        await callback.message.edit_text(
            "Состояние доступа изменилось. Вернитесь в главное меню и повторите действие.",
            reply_markup=back_to_main_keyboard(),
        )
        return
    edit_callback = "buy:renew" if quote.order_kind == ORDER_KIND_EXTEND else "buy:resume"
    await _show_quote(
        callback,
        quote,
        database=database,
        settings=settings,
        edit_callback=edit_callback,
    )


async def _show_quote(
    callback: CallbackQuery,
    quote: PurchaseQuote,
    database: Database,
    settings: Settings,
    *,
    edit_callback: str,
) -> None:
    if callback.message is None:
        return
    if quote.order_kind == ORDER_KIND_UPGRADE_DEVICES:
        title = "Проверьте увеличение лимита"
    elif quote.order_kind == ORDER_KIND_EXTEND:
        title = "Проверьте продление"
    elif quote.order_kind == ORDER_KIND_RESUME:
        title = "Проверьте возобновление"
    else:
        title = "Проверьте подключение"

    lines = [f"<b>{title}</b>", ""]
    if quote.order_kind == ORDER_KIND_PURCHASE:
        lines.extend(
            [
                f"Устройства: <b>{devices_ru(quote.max_devices)}</b>",
                f"Срок: <b>{days_ru(quote.duration_days)}</b>",
            ]
        )
    elif quote.order_kind in {ORDER_KIND_EXTEND, ORDER_KIND_RESUME}:
        expiration = calculate_expiration_snapshot(
            current_expires_at_utc=quote.base_valid_until_utc,
            captured_now_utc=quote.created_at_utc,
            purchased_duration_days=quote.requested_duration_days or quote.duration_days,
            order_kind=quote.order_kind,
            business_timezone=settings.subscription_time_zone,
            configured_policy_version=settings.expiration_policy_version,
            policy_effective_at_utc=settings.expiration_policy_effective_at_utc,
        )
        access_through = format_access_through_date_ru(
            expiration.target_expires_at_utc, settings.subscription_time_zone
        )
        lines.extend(
            [
                f"Текущий лимит: <b>{devices_ru(quote.max_devices)}</b>",
                "Добавится: <b>"
                f"{days_ru(quote.requested_duration_days or quote.duration_days)}</b>",
                f"Новая дата окончания: <b>{access_through}</b>",
            ]
        )
    if quote.personal_discount_bps > 0:
        lines.extend(
            [
                f"Персональная скидка: <b>{quote.personal_discount_bps / 100:g}%</b>",
                "Скидка: "
                "<b>−"
                f"{format_price(quote.personal_discount_amount_minor_units, quote.currency)}</b>",
            ]
        )
    sbp_url: str | None = None
    sbp_amount: int | None = None
    if quote.amount_minor_units > 0 and settings.external_payment_enabled:
        sbp_offer = PricingService(settings).calculate_quote_offer(quote, PAYMENT_MODE_YOOKASSA_SBP)
        sbp_amount = sbp_offer.amount_minor_units
        token = CheckoutTokenCodec(settings.checkout_token_secret.get_secret_value()).issue(
            quote.public_quote_id,
            int((quote.expires_at_utc + timedelta(minutes=settings.order_ttl_minutes)).timestamp()),
            amount_minor_units=sbp_offer.amount_minor_units,
            pricing_version=sbp_offer.pricing_version,
        )
        sbp_url = f"{settings.checkout_public_base_url}/checkout/{token}"
    if quote.amount_minor_units == 0:
        lines.append("Итого: <b>Бесплатно</b>")
    elif sbp_amount is not None:
        lines.append(
            "К оплате: <b>"
            f"{format_price(quote.amount_minor_units, quote.currency)}</b> или "
            f"<b>{format_price(sbp_amount, 'RUB')}</b> по СБП"
        )
        lines.append("Разовый платёж. Автопродления нет.")
    else:
        lines.append(f"К оплате: <b>{format_price(quote.amount_minor_units, quote.currency)}</b>")
    lines.append(f"Расчёт действует до: <b>{format_datetime(quote.expires_at_utc)}</b>")
    await callback.message.edit_text(
        "\n".join(lines),
        reply_markup=quote_confirmation_keyboard(
            quote.public_quote_id,
            edit_callback=edit_callback,
            complimentary=quote.amount_minor_units == 0,
            sbp_url=sbp_url,
            stars_amount=quote.amount_minor_units,
            sbp_amount_minor_units=sbp_amount,
        ),
    )
    await _record_quote_shown(database, quote)


@router.callback_query(F.data.startswith("buy:pay:"))
async def handle_quote_pay(
    callback: CallbackQuery,
    database: Database,
    settings: Settings,
    mediator_client: MediatorClient,
    readiness_service: CommerceReadinessService,
    bot_registry: BotRegistry,
    bot_key: str | None = None,
) -> None:
    await callback.answer()
    if callback.message is None or callback.data is None:
        return

    public_quote_id = callback.data.rsplit(":", maxsplit=1)[-1]
    async with database.session() as session:
        user = await UserRepository(session).get_by_telegram_id(callback.from_user.id)
        quote = (
            await PurchaseQuoteRepository(session).get_by_public_id_for_user(
                public_quote_id,
                user.id,
            )
            if user is not None
            else None
        )
    if quote is None:
        await callback.message.edit_text(
            "Расчёт не найден. Создайте новый заказ.",
            reply_markup=back_to_main_keyboard(),
        )
        return
    if not await _check_readiness(
        callback,
        readiness_service,
        operation_kind=_commerce_operation_for_order_kind(quote.order_kind),
        force=True,
    ):
        return
    await _complete_quote_payment(
        callback,
        database=database,
        settings=settings,
        mediator_client=mediator_client,
        bot_registry=bot_registry,
        public_quote_id=public_quote_id,
        bot_key=bot_key,
    )


async def _complete_quote_payment(
    callback: CallbackQuery,
    *,
    database: Database,
    settings: Settings,
    mediator_client: MediatorClient,
    bot_registry: BotRegistry,
    public_quote_id: str,
    bot_key: str | None,
) -> None:
    if callback.from_user is None or callback.message is None:
        return

    try:
        async with database.session() as session:
            service = PurchaseService(session, settings, mediator_client)
            order = await service.create_order_from_quote(
                public_quote_id,
                callback.from_user.id,
                payment_bot_key=bot_key,
                payment_provider=PAYMENT_MODE_TELEGRAM_STARS,
            )
            invoice = _build_invoice_if_required(service, order, settings)
    except ValueError:
        await callback.message.edit_text(
            "Расчёт устарел или состояние VPN изменилось. Создайте новый заказ.",
            reply_markup=back_to_main_keyboard(),
        )
        return

    if order.amount_minor_units == 0:
        await _activate_complimentary_order(
            callback,
            database,
            settings,
            mediator_client,
            order.id,
            callback.from_user.id,
        )
        return

    await _send_order_invoice(callback, order, invoice, settings, bot_registry)


@router.callback_query(F.data.startswith("order:continue:"))
async def handle_continue_order(
    callback: CallbackQuery,
    database: Database,
    settings: Settings,
    mediator_client: MediatorClient,
    readiness_service: CommerceReadinessService,
    bot_registry: BotRegistry,
    bot_key: str | None = None,
) -> None:
    await callback.answer()
    if callback.from_user is None or callback.message is None or callback.data is None:
        return
    if not await _check_readiness(callback, readiness_service, force=True):
        return
    await _complete_existing_order_payment(
        callback,
        database=database,
        settings=settings,
        mediator_client=mediator_client,
        bot_registry=bot_registry,
        public_order_id=callback.data.split(":", maxsplit=2)[2],
        bot_key=bot_key,
    )


async def _complete_existing_order_payment(
    callback: CallbackQuery,
    *,
    database: Database,
    settings: Settings,
    mediator_client: MediatorClient,
    bot_registry: BotRegistry,
    public_order_id: str,
    bot_key: str | None,
) -> None:
    if callback.from_user is None or callback.message is None:
        return
    async with database.session() as session:
        user = await UserRepository(session).get_by_telegram_id(callback.from_user.id)
        order = (
            await OrderRepository(session).get_by_public_id_for_user(public_order_id, user.id)
            if user is not None
            else None
        )
        service = PurchaseService(session, settings, mediator_client)
        order_is_valid = False
        sbp_checkout_url: str | None = None
        if order is not None and order.status == ORDER_STATUS_PENDING:
            if order.provider == PAYMENT_MODE_YOOKASSA_SBP:
                quote = (
                    await session.get(PurchaseQuote, order.quote_id)
                    if order.quote_id is not None
                    else None
                )
                if (
                    settings.external_payment_enabled
                    and quote is not None
                    and order.expires_at_utc is not None
                    and to_aware_utc(order.expires_at_utc) > utc_now()
                ):
                    token = CheckoutTokenCodec(
                        settings.checkout_token_secret.get_secret_value()
                    ).issue(
                        quote.public_quote_id,
                        int(order.expires_at_utc.timestamp()),
                        amount_minor_units=order.amount_minor_units,
                        pricing_version=order.pricing_version,
                    )
                    sbp_checkout_url = f"{settings.checkout_public_base_url}/checkout/{token}"
                    order_is_valid = True
            else:
                if order.payment_bot_key is None and bot_key is not None:
                    await OrderRepository(session).claim_payment_bot(order, bot_key)
                validated_order, _ = await service.validate_order_for_invoice(
                    payload=order.invoice_payload,
                    amount_minor_units=order.amount_minor_units,
                    currency=order.currency,
                    payer_telegram_id=callback.from_user.id,
                )
                order_is_valid = validated_order is not None
        invoice = (
            _build_invoice_if_required(service, order, settings)
            if order is not None and order_is_valid
            else None
        )
    if order is None or not order_is_valid:
        await callback.message.edit_text(
            "Незавершённый заказ уже недоступен.",
            reply_markup=back_to_main_keyboard(),
        )
        return

    if order.amount_minor_units == 0:
        await _activate_complimentary_order(
            callback,
            database,
            settings,
            mediator_client,
            order.id,
            callback.from_user.id,
        )
        return

    if sbp_checkout_url is not None:
        await callback.message.edit_text(
            "<b>Оплата по СБП не завершена</b>\n\n"
            f"К оплате: <b>{format_price(order.amount_minor_units, order.currency)}</b>\n"
            "Разовый платёж. Автопродления нет.",
            reply_markup=continue_sbp_keyboard(sbp_checkout_url),
        )
        return

    await _send_order_invoice(callback, order, invoice, settings, bot_registry)


async def _activate_complimentary_order(
    callback: CallbackQuery,
    database: Database,
    settings: Settings,
    mediator_client: MediatorClient,
    order_id: int,
    actor_telegram_id: int,
) -> None:
    if callback.message is None:
        return

    try:
        async with database.session() as session:
            prepared = await PurchaseService(
                session,
                settings,
                mediator_client,
            ).prepare_complimentary_order_for_activation(
                order_id,
                actor_telegram_id,
            )

        if prepared.already_paid and prepared.subscription is not None:
            subscription = prepared.subscription
        else:
            async with database.session() as session:
                activation = await PurchaseService(
                    session,
                    settings,
                    mediator_client,
                ).activate_order_by_id(order_id)

            if activation.failure_code is not None or activation.subscription is None:
                await callback.message.edit_text(
                    "Бесплатный заказ сохранён, но доступ пока не активировался. "
                    "Повторите активацию из главного меню или откройте поддержку.",
                    reply_markup=back_to_main_keyboard(),
                )
                return

            subscription = activation.subscription
    except ValueError:
        await callback.message.edit_text(
            "Бесплатный заказ уже недоступен. Создайте новый расчёт.",
            reply_markup=back_to_main_keyboard(),
        )
        return

    await callback.message.edit_text(
        "<b>Доступ активирован бесплатно</b>\n\n"
        f"Действует до: <b>{format_datetime(subscription.expires_at)}</b>\n"
        f"Лимит устройств: <b>{subscription.max_devices}</b>\n\n"
        "Осталось подключить это устройство.",
        reply_markup=after_purchase_keyboard(),
    )


async def _send_order_invoice(
    callback: CallbackQuery,
    order: Order,
    invoice: TelegramStarsInvoice | None,
    settings: Settings,
    bot_registry: BotRegistry,
) -> None:
    if callback.message is None or callback.from_user is None:
        return
    if settings.payment_mode == PAYMENT_MODE_MANUAL:
        await callback.message.edit_text(
            "<b>Заявка на оплату создана</b>\n\n"
            f"Номер заказа: <code>{order.public_order_id}</code>\n\n"
            "Администратор проверит оплату и активирует доступ.",
            reply_markup=back_to_main_keyboard(),
        )
        return
    if invoice is None or order.payment_bot_key is None:
        await callback.message.edit_text(
            "Не удалось создать счёт. Оплата не списана.",
            reply_markup=back_to_main_keyboard(),
        )
        return
    runtime = bot_registry.get(order.payment_bot_key)
    if runtime is None or runtime.bot is None:
        await callback.message.edit_text(
            "Бот, через который выставлен счёт, временно недоступен. Повторите попытку позже.",
            reply_markup=back_to_main_keyboard(),
        )
        return
    await runtime.bot.send_invoice(
        chat_id=callback.from_user.id,
        title=invoice.title,
        description=invoice.description,
        payload=invoice.payload,
        provider_token=invoice.provider_token,
        currency=invoice.currency,
        prices=[LabeledPrice(label=invoice.prices[0].label, amount=invoice.prices[0].amount)],
    )
