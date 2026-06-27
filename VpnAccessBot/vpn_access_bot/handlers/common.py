from __future__ import annotations

from aiogram import F, Router
from aiogram.dispatcher.event.bases import SkipHandler
from aiogram.filters import Command, CommandStart
from aiogram.types import CallbackQuery, Message
from aiogram.types import User as TelegramUser

from vpn_access_bot.advertising_readiness import (
    AcquisitionService,
    CommercePolicyRepository,
)
from vpn_access_bot.commerce import CabinetState
from vpn_access_bot.config import Settings
from vpn_access_bot.constants import (
    ORDER_STATUS_ACTIVATION_FAILED,
    ORDER_STATUS_PAID,
    ORDER_STATUS_PAYMENT_RECEIVED,
)
from vpn_access_bot.db import Database
from vpn_access_bot.formatting import escape_html
from vpn_access_bot.home import HomeStateService
from vpn_access_bot.keyboards import (
    back_to_main_keyboard,
    back_to_more_keyboard,
    main_menu_keyboard,
    more_menu_keyboard,
    onboarding_access_keyboard,
    payment_help_keyboard,
    support_categories_keyboard,
)
from vpn_access_bot.mediator_client import MediatorClient
from vpn_access_bot.models import utc_now
from vpn_access_bot.product_completion import bind_referrer_from_payload
from vpn_access_bot.product_events import ProductEventName
from vpn_access_bot.readiness import CommerceReadinessService
from vpn_access_bot.repositories import (
    AuditRepository,
    OrderRepository,
    ProductEventRepository,
    SupportRepository,
    UserRepository,
)
from vpn_access_bot.support_security import contains_secret_material
from vpn_access_bot.telegram.context import get_bot_key
from vpn_access_bot.texts import happ_instruction_text, pay_support_text, support_text
from vpn_access_bot.user_texts import cabinet_text

router = Router(name="common")


def _support_unavailable_text(settings: Settings) -> str:
    contact = (getattr(settings, "support_contact", None) or "").strip()
    if contact:
        return (
            "Встроенная поддержка временно недоступна. "
            f"Напишите напрямую: <b>{escape_html(contact)}</b>."
        )
    return "Поддержка временно недоступна. Попробуйте позже."


async def _build_main_menu_state(
    telegram_user: TelegramUser,
    database: Database,
    settings: Settings,
    mediator_client: MediatorClient,
    readiness_service: CommerceReadinessService | None = None,
) -> CabinetState:
    readiness_service = readiness_service or CommerceReadinessService(
        mediator_client,
        settings.readiness_cache_seconds,
    )
    return await HomeStateService(
        database,
        settings,
        mediator_client,
        readiness_service,
    ).build(telegram_user)


async def _answer_main_menu(
    message: Message,
    database: Database,
    settings: Settings,
    mediator_client: MediatorClient,
    readiness_service: CommerceReadinessService,
) -> None:
    if message.from_user is None:
        return
    state = await _build_main_menu_state(
        message.from_user,
        database,
        settings,
        mediator_client,
        readiness_service,
    )
    await message.answer(
        cabinet_text(state, settings.product_name, settings.subscription_time_zone),
        reply_markup=main_menu_keyboard(state),
    )


@router.message(CommandStart())
async def handle_start(
    message: Message,
    database: Database,
    settings: Settings,
    mediator_client: MediatorClient,
    readiness_service: CommerceReadinessService,
) -> None:
    if message.from_user is not None:
        parts = (message.text or "").split(maxsplit=1)
        payload = parts[1] if len(parts) == 2 else None

        async with database.session() as session:
            user = await UserRepository(session).get_or_create_from_message_user(
                telegram_id=message.from_user.id,
                username=message.from_user.username,
                first_name=message.from_user.first_name,
            )
            await bind_referrer_from_payload(session, user, payload)
            policy = await CommercePolicyRepository(session).get()
            campaign = None
            if policy.campaign_tracking_enabled:
                campaign = await AcquisitionService(session).record_start(
                    user_id=user.id,
                    payload=payload,
                    bot_key=get_bot_key(),
                )
            event_repository = ProductEventRepository(session)
            await event_repository.record(
                event_name=ProductEventName.FIRST_START,
                user_id=user.id,
                idempotency_key=f"first-start:{user.id}",
                payload={"source": "start_command"},
            )
            if campaign is not None:
                await event_repository.record(
                    event_name=ProductEventName.CAMPAIGN_TOUCH,
                    user_id=user.id,
                    idempotency_key=(f"campaign-touch:{user.id}:{campaign.id}:start"),
                    payload={"source": "start_command"},
                )

    await _answer_main_menu(message, database, settings, mediator_client, readiness_service)


@router.message(Command("menu"))
async def handle_menu(
    message: Message,
    database: Database,
    settings: Settings,
    mediator_client: MediatorClient,
    readiness_service: CommerceReadinessService,
) -> None:
    await _answer_main_menu(
        message,
        database,
        settings,
        mediator_client,
        readiness_service,
    )


@router.message(Command("paysupport"))
async def handle_pay_support(message: Message, database: Database) -> None:
    if message.from_user is None:
        return

    recent_order = None

    async with database.session() as session:
        user = await UserRepository(session).get_by_telegram_id(message.from_user.id)

        if user is not None:
            orders = await OrderRepository(session).get_recent_for_user_by_statuses(
                user_id=user.id,
                statuses=[
                    ORDER_STATUS_ACTIVATION_FAILED,
                    ORDER_STATUS_PAYMENT_RECEIVED,
                    ORDER_STATUS_PAID,
                ],
            )
            recent_order = orders[0] if orders else None

    await message.answer(pay_support_text(recent_order), reply_markup=back_to_main_keyboard())


@router.callback_query(F.data.startswith("order:payment_help:"))
async def handle_order_payment_help(
    callback: CallbackQuery,
    database: Database,
) -> None:
    await callback.answer()
    if callback.from_user is None or callback.message is None or callback.data is None:
        return

    public_order_id = callback.data.split(":", maxsplit=2)[2]
    async with database.session() as session:
        user = await UserRepository(session).get_by_telegram_id(callback.from_user.id)
        order = (
            await OrderRepository(session).get_by_public_id_for_user(public_order_id, user.id)
            if user is not None
            else None
        )

    await callback.message.edit_text(
        pay_support_text(order),
        reply_markup=payment_help_keyboard(),
    )


@router.callback_query(F.data == "menu:main")
async def handle_main_menu(
    callback: CallbackQuery,
    database: Database,
    settings: Settings,
    mediator_client: MediatorClient,
    readiness_service: CommerceReadinessService,
) -> None:
    await callback.answer()

    if callback.from_user is None or callback.message is None:
        return

    state = await _build_main_menu_state(
        callback.from_user,
        database,
        settings,
        mediator_client,
        readiness_service,
    )
    await callback.message.edit_text(
        cabinet_text(state, settings.product_name, settings.subscription_time_zone),
        reply_markup=main_menu_keyboard(state),
    )


@router.callback_query(F.data == "menu:more")
async def handle_more_menu(callback: CallbackQuery) -> None:
    await callback.answer()
    if callback.message is None:
        return
    await callback.message.edit_text(
        "<b>Дополнительные действия</b>",
        reply_markup=more_menu_keyboard(),
    )


@router.callback_query(F.data == "about:show")
async def handle_about(callback: CallbackQuery, settings: Settings) -> None:
    await callback.answer()
    if callback.message is None:
        return
    await callback.message.edit_text(
        f"<b>{escape_html(settings.product_name)}</b>\n\n"
        "Простой VPN для телефона, компьютера и телевизора. "
        "Управление сроком и устройствами находится прямо в этом боте.\n\n"
        "Для подключения используется приложение Happ.",
        reply_markup=back_to_more_keyboard(),
    )


@router.callback_query(F.data == "help:happ")
async def handle_happ_help(callback: CallbackQuery, settings: Settings) -> None:
    await callback.answer()

    if callback.message is None:
        return

    await callback.message.edit_text(
        happ_instruction_text(settings.product_name),
        reply_markup=onboarding_access_keyboard(settings.product_name),
    )


@router.callback_query(F.data == "support:show")
async def handle_support(
    callback: CallbackQuery,
    database: Database,
    settings: Settings,
) -> None:
    await callback.answer()

    if callback.message is None:
        return

    if callback.from_user is not None:
        async with database.session() as session:
            user = await UserRepository(session).get_by_telegram_id(callback.from_user.id)
            if user is not None:
                await ProductEventRepository(session).record(
                    event_name="support_opened",
                    user_id=user.id,
                )

    await callback.message.edit_text(
        support_text(settings.product_name),
        reply_markup=support_categories_keyboard(),
    )


@router.callback_query(F.data.startswith("support:cat:"))
async def handle_support_category(
    callback: CallbackQuery,
    database: Database,
    settings: Settings,
) -> None:
    await callback.answer()

    if callback.from_user is None or callback.message is None or callback.data is None:
        return

    category = callback.data.rsplit(":", maxsplit=1)[-1]
    username = f"@{callback.from_user.username}" if callback.from_user.username else "нет"
    if settings.support_chat_id is None:
        await callback.message.edit_text(
            _support_unavailable_text(settings),
            reply_markup=back_to_main_keyboard(),
        )
        return

    diagnostic = (
        "<b>Запрос поддержки</b>\n\n"
        f"Категория: <b>{category}</b>\n"
        f"Telegram ID: <code>{callback.from_user.id}</code>\n"
        f"Username: <b>{username}</b>"
    )

    try:
        root_message = await callback.bot.send_message(settings.support_chat_id, diagnostic)
    except Exception:
        await callback.message.edit_text(
            _support_unavailable_text(settings),
            reply_markup=back_to_main_keyboard(),
        )
        return

    async with database.session() as session:
        user = await UserRepository(session).get_or_create_from_message_user(
            telegram_id=callback.from_user.id,
            username=callback.from_user.username,
            first_name=callback.from_user.first_name,
        )
        support_repository = SupportRepository(session)
        request = await support_repository.create_request(
            user=user,
            category=category,
            support_chat_id=settings.support_chat_id,
            support_root_message_id=root_message.message_id,
        )
        await support_repository.add_message(
            request=request,
            direction="support",
            telegram_chat_id=settings.support_chat_id,
            telegram_message_id=root_message.message_id,
            message_type="header",
        )

    await callback.message.edit_text(
        f"Мы получили запрос №{request.public_id[:8]}.\n\n"
        "Опишите проблему одним сообщением и приложите скриншот, "
        "если он есть. Секретные ссылки и коды подключения присылать не нужно.",
        reply_markup=back_to_main_keyboard(),
    )


@router.message(F.reply_to_message)
async def handle_support_admin_reply(
    message: Message,
    database: Database,
    settings: Settings,
) -> None:
    if settings.support_chat_id is None or message.chat.id != settings.support_chat_id:
        raise SkipHandler

    if (
        message.reply_to_message is None
        or message.from_user is None
        or message.from_user.id not in settings.support_agent_telegram_ids
    ):
        return

    if contains_secret_material(message.text or message.caption):
        await message.answer(
            "Ответ заблокирован: сообщение похоже на секретную ссылку или токен. "
            "Передайте пользователю только безопасную инструкцию."
        )
        return

    async with database.session() as session:
        support_repository = SupportRepository(session)
        request = await support_repository.find_request_by_support_message(
            telegram_chat_id=message.chat.id,
            telegram_message_id=message.reply_to_message.message_id,
        )

        if request is None:
            return

        user = await UserRepository(session).get_by_id(request.user_id)

        if user is None:
            return

    try:
        await message.copy_to(chat_id=user.telegram_id)
    except Exception:
        await message.answer("Не удалось отправить ответ пользователю.")
        return

    async with database.session() as session:
        support_repository = SupportRepository(session)
        refreshed = await support_repository.find_request_by_support_message(
            telegram_chat_id=message.chat.id,
            telegram_message_id=message.reply_to_message.message_id,
        )

        if refreshed is not None:
            await support_repository.add_message(
                request=refreshed,
                direction="support",
                telegram_chat_id=message.chat.id,
                telegram_message_id=message.message_id,
                message_type=message.content_type,
            )
            refreshed.status = "waiting_user_reply"
            refreshed.updated_at_utc = utc_now()
            await AuditRepository(session).add(
                event_type="support.reply_sent",
                telegram_id=message.from_user.id,
                details_json=f'{{"request":"{refreshed.public_id}"}}',
            )

    await message.answer("Ответ отправлен пользователю.")


@router.message()
async def handle_open_support_user_message(
    message: Message,
    database: Database,
    settings: Settings,
) -> None:
    if message.from_user is None or settings.support_chat_id is None:
        return

    if message.chat.id == settings.support_chat_id:
        return

    if message.text is not None and message.text.startswith("/"):
        return

    if contains_secret_material(message.text or message.caption):
        await message.answer(
            "Не отправляйте в поддержку ссылку подключения или токен. "
            "Пришлите только текст ошибки и скриншот без ссылки."
        )
        return

    async with database.session() as session:
        user = await UserRepository(session).get_by_telegram_id(message.from_user.id)

        if user is None:
            return

        support_repository = SupportRepository(session)
        request = await support_repository.get_open_for_user(user.id)

        if request is None:
            return

        request_id = request.id

    try:
        copied_message = await message.copy_to(settings.support_chat_id)
    except Exception:
        await message.answer(_support_unavailable_text(settings))
        return

    async with database.session() as session:
        support_repository = SupportRepository(session)
        request = await support_repository.get_by_id(request_id)

        if request is None:
            return

        await support_repository.add_message(
            request=request,
            direction="user",
            telegram_chat_id=settings.support_chat_id,
            telegram_message_id=copied_message.message_id,
            message_type=message.content_type,
        )
        request.status = "waiting_support"
        request.updated_at_utc = utc_now()

    await message.answer(
        f"Сообщение отправлено в поддержку.\nНомер обращения: {request.public_id[:8]}"
    )
