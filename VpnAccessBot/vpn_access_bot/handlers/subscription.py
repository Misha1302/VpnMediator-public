from __future__ import annotations

import logging

from aiogram import F, Router
from aiogram.types import CallbackQuery, LinkPreviewOptions

from vpn_access_bot.config import Settings
from vpn_access_bot.constants import SUBSCRIPTION_STATUS_DISABLED
from vpn_access_bot.credential_delivery import build_delivery_plan
from vpn_access_bot.db import Database
from vpn_access_bot.formatting import (
    escape_html,
    format_access_through_date_ru,
    format_datetime_ru,
    format_iso_datetime_ru,
)
from vpn_access_bot.keyboards import (
    back_to_main_keyboard,
    credential_delivery_keyboard,
    device_action_confirm_keyboard,
    device_bulk_management_keyboard,
    device_management_keyboard,
    devices_keyboard,
    reset_confirm_keyboard,
    subscription_keyboard,
)
from vpn_access_bot.mediator_client import MediatorClient, MediatorClientError
from vpn_access_bot.models import utc_now
from vpn_access_bot.repositories import (
    DeviceResetRepository,
    OnboardingSessionRepository,
    SubscriptionRepository,
    UserRepository,
    to_aware_utc,
)

router = Router(name="subscription")
logger = logging.getLogger(__name__)


def _device_label(device, fallback: str) -> str:
    if device.detected_model:
        return device.detected_model

    base = device.display_name or fallback
    suffix = device.public_id[-4:] if device.public_id else ""
    return f"{base} · …{suffix}" if suffix else base


def _visible_devices(devices):
    return [
        device
        for device in devices
        if device.access_channel == "unified_feed" or device.state != "revoked"
    ]


def _active_device_line(device, fallback: str) -> str:
    label = escape_html(_device_label(device, fallback))
    platform = f" ({_platform_label(device.platform)})" if device.platform else ""
    last_used = format_iso_datetime_ru(device.last_used_at_utc, fallback="нет данных")
    return f"📱 {label}{platform}\n   Последняя активность: {last_used}"


def _platform_label(platform: str | None) -> str:
    return {
        "android": "Android",
        "ios": "iPhone / iPad",
        "windows": "Windows",
        "macos": "macOS",
        "linux": "Linux",
        "android_tv": "Android TV",
        "apple_tv": "Apple TV",
        "router": "роутер",
    }.get(platform or "", "устройство")


def _log_mediator_error(
    action: str,
    callback: CallbackQuery,
    exception: MediatorClientError,
) -> None:
    telegram_id = callback.from_user.id if callback.from_user is not None else None
    logger.warning(
        "Mediator action failed: action=%s telegram_id=%s status_code=%s error_code=%s",
        action,
        telegram_id,
        exception.status_code,
        exception.error_code,
        exc_info=True,
    )


async def _load_subscription_context(database: Database, telegram_id: int):
    async with database.session() as session:
        user = await UserRepository(session).get_by_telegram_id(telegram_id)
        subscription = (
            await SubscriptionRepository(session).get_primary_for_user(user)
            if user is not None
            else None
        )
        return user, subscription


def _subscription_status_label(subscription, remote_is_active: bool | None) -> str:
    if subscription.reconciliation_state == "blocked":
        return "требует проверки"
    if subscription.reconciliation_state == "recovering":
        return "восстанавливается"
    if remote_is_active is False:
        return "требует проверки"
    expires_at = to_aware_utc(subscription.expires_at)
    if subscription.status == SUBSCRIPTION_STATUS_DISABLED:
        return "отключён"
    if expires_at <= utc_now():
        return "истёк"
    return "активен"


async def _show_service_unavailable(callback: CallbackQuery) -> None:
    if callback.message is None:
        return
    await callback.message.edit_text(
        "Не удалось проверить состояние доступа и устройств.\n\n"
        "Попробуйте ещё раз через минуту. Повторно оплачивать доступ не нужно.",
        reply_markup=back_to_main_keyboard(),
    )


async def _render_devices(
    callback: CallbackQuery,
    subscription,
    mediator_client: MediatorClient,
) -> None:
    if callback.message is None:
        return
    try:
        devices = await mediator_client.list_device_tokens(subscription.public_guid)
    except MediatorClientError as exception:
        _log_mediator_error("list_device_tokens", callback, exception)
        await _show_service_unavailable(callback)
        return

    visible_devices = _visible_devices(devices)
    active_devices = [device for device in visible_devices if device.state == "active"]
    pending_devices = [device for device in visible_devices if device.state == "pending"]
    disabled_devices = [device for device in visible_devices if device.state == "revoked"]
    lines: list[str] = []

    if active_devices:
        lines.append("<b>Подключённые устройства</b>")
        lines.extend(
            _active_device_line(device, f"Устройство {index}")
            for index, device in enumerate(active_devices, start=1)
        )
    if pending_devices:
        if lines:
            lines.append("")
        lines.append("<b>Ожидают подключения</b>")
        lines.extend(
            "⏳ "
            f"{escape_html(device.display_name or f'Новое устройство {index}')} — "
            f"до {format_iso_datetime_ru(device.pending_expires_at_utc)}"
            for index, device in enumerate(pending_devices, start=1)
        )
    if disabled_devices:
        if lines:
            lines.append("")
        lines.append("<b>Отключённые устройства</b>")
        lines.extend(
            f"○ {escape_html(_device_label(device, f'Устройство {index}'))}"
            for index, device in enumerate(disabled_devices, start=1)
        )
    if not lines:
        lines.append("Пока нет подключённых устройств.")

    buttons = [
        (device.public_id, _device_label(device, f"устройство {index}"))
        for index, device in enumerate(active_devices, start=1)
    ]
    buttons.extend(
        (device.public_id, device.display_name or "новое устройство") for device in pending_devices
    )
    buttons.extend(
        (device.public_id, _device_label(device, f"устройство {index}"))
        for index, device in enumerate(disabled_devices, start=1)
    )

    await callback.message.edit_text(
        "<b>Мои устройства</b>\n\n"
        + "\n".join(lines)
        + f"\n\nПодключено устройств: <b>{len(active_devices)} "
        f"из {subscription.max_devices}</b>",
        reply_markup=devices_keyboard(buttons),
    )


async def _send_subscription_credential(
    callback: CallbackQuery,
    connection_url: str,
    settings: Settings,
) -> None:
    if callback.message is None:
        return
    delivery = build_delivery_plan(
        connection_url,
        happ_deep_link_template=settings.happ_deep_link_template,
        primary_subscription_base_url=settings.public_subscription_base_url,
        fallback_subscription_base_url=settings.fallback_subscription_base_url,
    )
    copy_note = (
        "Нажмите «Скопировать ссылку» и вставьте её в Happ."
        if delivery.can_copy
        else "Нажмите и удерживайте ссылку, скопируйте её целиком и вставьте в Happ."
    )
    await callback.message.answer(
        "<b>Ссылка подписки</b>\n\n"
        f"<code>{escape_html(delivery.connection_url)}</code>\n\n"
        f"{copy_note} Эту же ссылку можно использовать на других ваших устройствах.",
        reply_markup=credential_delivery_keyboard(
            delivery.connection_url,
            happ_deep_link=delivery.happ_deep_link,
            can_copy=delivery.can_copy,
        ),
        link_preview_options=LinkPreviewOptions(is_disabled=True),
    )


@router.callback_query(F.data == "subscription:show")
async def handle_subscription_show(
    callback: CallbackQuery,
    database: Database,
    mediator_client: MediatorClient,
    settings: Settings,
) -> None:
    await callback.answer()
    if callback.from_user is None or callback.message is None:
        return

    _, subscription = await _load_subscription_context(database, callback.from_user.id)
    if subscription is None:
        await callback.message.edit_text(
            "<b>Доступа пока нет</b>\n\nАктивируйте бесплатный период или купите доступ.",
            reply_markup=subscription_keyboard(False),
        )
        return

    active_count: int | None = None
    max_devices = subscription.max_devices
    remote_is_active: bool | None = None
    remote_checked = False
    try:
        details = await mediator_client.get_subscription(subscription.public_guid)
        active_count = details.active_device_count
        max_devices = details.max_devices
        remote_is_active = details.is_active
        remote_checked = True
    except MediatorClientError as exception:
        _log_mediator_error("get_subscription", callback, exception)

    expires_at = to_aware_utc(subscription.expires_at)
    status = _subscription_status_label(subscription, remote_is_active)

    device_line = (
        f"Подключено устройств: <b>{active_count} из {max_devices}</b>"
        if active_count is not None
        else f"Лимит устройств: <b>{max_devices}</b>\n"
        "Число подключённых устройств временно не удалось проверить."
    )
    access_through = format_access_through_date_ru(expires_at, settings.subscription_time_zone)
    notices: list[str] = []
    if subscription.reconciliation_state in {"blocked", "recovering"}:
        notices.append(
            "Оплата и изменения временно недоступны, пока состояние доступа синхронизируется. "
            "Повторно оплачивать не нужно."
        )
    elif not remote_checked:
        notices.append("Состояние сервиса временно не удалось проверить.")
    elif remote_is_active is False:
        notices.append("Удалённый сервис не подтверждает активный доступ. Состояние проверяется.")
    notice_text = f"\n\n{' '.join(notices)}" if notices else ""
    await callback.message.edit_text(
        f"<b>{escape_html(settings.product_name)}</b>\n\n"
        f"Статус: <b>{status}</b>\n"
        f"Действует до: <b>{access_through}</b>\n"
        f"{device_line}{notice_text}",
        reply_markup=subscription_keyboard(True),
    )


@router.callback_query(F.data == "subscription:devices")
async def handle_devices_list(
    callback: CallbackQuery,
    database: Database,
    mediator_client: MediatorClient,
) -> None:
    await callback.answer()
    if callback.from_user is None or callback.message is None:
        return

    _, subscription = await _load_subscription_context(database, callback.from_user.id)
    if subscription is None:
        await callback.message.edit_text(
            "Сначала активируйте доступ к VPN.",
            reply_markup=subscription_keyboard(False),
        )
        return

    await _render_devices(callback, subscription, mediator_client)


@router.callback_query(F.data == "subscription:devices_manage")
async def handle_device_bulk_management(callback: CallbackQuery) -> None:
    await callback.answer()
    if callback.message is None:
        return
    await callback.message.edit_text(
        "<b>Управление устройствами</b>\n\n"
        "Сброс отключит все текущие ссылки. Подключать устройства придётся заново.",
        reply_markup=device_bulk_management_keyboard(),
    )


@router.callback_query(F.data.startswith("device:manage:"))
async def handle_device_manage(
    callback: CallbackQuery,
    database: Database,
    mediator_client: MediatorClient,
) -> None:
    await callback.answer()
    if callback.from_user is None or callback.message is None or callback.data is None:
        return
    _, subscription = await _load_subscription_context(database, callback.from_user.id)
    if subscription is None:
        await callback.message.edit_text(
            "Подписка не найдена.", reply_markup=back_to_main_keyboard()
        )
        return
    device_public_id = callback.data.split(":", maxsplit=2)[2]
    try:
        devices = await mediator_client.list_device_tokens(subscription.public_guid)
    except MediatorClientError as exception:
        _log_mediator_error("list_device_tokens", callback, exception)
        await _show_service_unavailable(callback)
        return
    device = next((item for item in devices if item.public_id == device_public_id), None)
    if device is None:
        await callback.message.edit_text(
            "Устройство не найдено.",
            reply_markup=devices_keyboard([]),
        )
        return
    protection = (
        "определяется по установке Happ"
        if device.access_channel == "unified_feed"
        else "требует проверки"
        if device.binding_state == "review"
        else "привязана к установке Happ"
        if device.feed_policy_version >= 2 and device.identity_bound
        else "ожидает привязки к установке Happ"
        if device.feed_policy_version >= 2 and device.feed_policy_mode == "enforce"
        else "усиленная по платформе"
        if device.feed_policy_mode == "enforce"
        else "обычная"
    )
    bound_platform = (
        f"\nПривязано к: <b>{escape_html(_platform_label(device.bound_platform))}</b>"
        if device.bound_platform
        else ""
    )
    await callback.message.edit_text(
        f"<b>{escape_html(_device_label(device, 'Устройство'))}</b>\n\n"
        f"Защита ссылки: <b>{protection}</b>{bound_platform}\n\n"
        "Выберите действие.",
        reply_markup=device_management_keyboard(
            device_public_id,
            can_transfer=False,
            disabled=device.state == "revoked",
        ),
    )


async def _deliver_shared_subscription_link(
    callback: CallbackQuery,
    database: Database,
    mediator_client: MediatorClient,
    settings: Settings,
) -> None:
    if callback.from_user is None or callback.message is None:
        return

    _, subscription = await _load_subscription_context(database, callback.from_user.id)
    if subscription is None:
        await callback.message.edit_text(
            "Подписка не найдена.",
            reply_markup=back_to_main_keyboard(),
        )
        return

    try:
        credential = await mediator_client.ensure_subscription_feed(subscription.public_guid)
    except MediatorClientError as exception:
        _log_mediator_error("ensure_subscription_feed", callback, exception)
        await _show_service_unavailable(callback)
        return

    await callback.message.edit_text(
        "<b>Теперь для всех устройств используется одна общая ссылка</b>\n\n"
        "Переносить или перевыпускать ссылку для отдельного устройства больше не нужно. "
        "Happ зарегистрирует каждую установку отдельно по её идентификатору.",
        reply_markup=subscription_keyboard(True),
    )
    await _send_subscription_credential(callback, credential.connection_url, settings)


@router.callback_query(F.data.startswith("device:transfer:"))
async def handle_device_transfer_platform(
    callback: CallbackQuery,
    database: Database,
    mediator_client: MediatorClient,
    settings: Settings,
) -> None:
    await callback.answer()
    await _deliver_shared_subscription_link(callback, database, mediator_client, settings)


@router.callback_query(F.data.startswith("device:move:"))
async def handle_device_transfer_confirm(
    callback: CallbackQuery,
    database: Database,
    mediator_client: MediatorClient,
    settings: Settings,
) -> None:
    await callback.answer()
    await _deliver_shared_subscription_link(callback, database, mediator_client, settings)


@router.callback_query(F.data.startswith("device:move_do:"))
async def handle_device_transfer_do(
    callback: CallbackQuery,
    database: Database,
    mediator_client: MediatorClient,
    settings: Settings,
) -> None:
    await callback.answer()
    await _deliver_shared_subscription_link(callback, database, mediator_client, settings)


@router.callback_query(F.data.startswith("device:credential:"))
async def handle_device_credential(
    callback: CallbackQuery,
    database: Database,
    mediator_client: MediatorClient,
    settings: Settings,
) -> None:
    await callback.answer()
    await _deliver_shared_subscription_link(callback, database, mediator_client, settings)


@router.callback_query(F.data.startswith("device:revoke:"))
async def handle_device_revoke_confirm(
    callback: CallbackQuery,
    database: Database,
    mediator_client: MediatorClient,
) -> None:
    await callback.answer()
    if callback.from_user is None or callback.message is None or callback.data is None:
        return
    _, subscription = await _load_subscription_context(database, callback.from_user.id)
    if subscription is None:
        return
    device_public_id = callback.data.split(":", maxsplit=2)[2]
    try:
        devices = await mediator_client.list_device_tokens(subscription.public_guid)
    except MediatorClientError as exception:
        _log_mediator_error("list_device_tokens", callback, exception)
        await _show_service_unavailable(callback)
        return
    if not any(device.public_id == device_public_id for device in devices):
        await _render_devices(callback, subscription, mediator_client)
        return
    await callback.message.edit_text(
        "<b>Запретить обновление подписки для устройства?</b>\n\n"
        "Ссылка и обновления подписки для этого устройства будут отключены. "
        "Остальные устройства не изменятся. Уже импортированный вручную профиль "
        "может сохраниться в стороннем клиенте.",
        reply_markup=device_action_confirm_keyboard(device_public_id),
    )


@router.callback_query(F.data.startswith("device:revoke_do:"))
async def handle_device_revoke_do(
    callback: CallbackQuery,
    database: Database,
    mediator_client: MediatorClient,
) -> None:
    await callback.answer()
    if callback.from_user is None or callback.message is None or callback.data is None:
        return
    _, subscription = await _load_subscription_context(database, callback.from_user.id)
    if subscription is None:
        return
    device_public_id = callback.data.split(":", maxsplit=2)[2]
    try:
        await mediator_client.revoke_device_token(subscription.public_guid, device_public_id)
    except MediatorClientError as exception:
        _log_mediator_error("revoke_device_token", callback, exception)
        await _show_service_unavailable(callback)
        return
    await _render_devices(callback, subscription, mediator_client)


@router.callback_query(F.data.startswith("device:enable:"))
async def handle_device_enable(
    callback: CallbackQuery,
    database: Database,
    mediator_client: MediatorClient,
) -> None:
    await callback.answer()
    if callback.from_user is None or callback.message is None or callback.data is None:
        return
    _, subscription = await _load_subscription_context(database, callback.from_user.id)
    if subscription is None:
        return
    device_public_id = callback.data.split(":", maxsplit=2)[2]
    try:
        await mediator_client.enable_unified_device(
            subscription.public_guid,
            device_public_id,
        )
    except MediatorClientError as exception:
        _log_mediator_error("enable_unified_device", callback, exception)
        await _show_service_unavailable(callback)
        return
    await _render_devices(callback, subscription, mediator_client)


@router.callback_query(F.data.startswith("device:regenerate:"))
async def handle_device_regenerate_confirm(
    callback: CallbackQuery,
    database: Database,
    mediator_client: MediatorClient,
    settings: Settings,
) -> None:
    await callback.answer()
    await _deliver_shared_subscription_link(callback, database, mediator_client, settings)


@router.callback_query(F.data.startswith("device:regenerate_do:"))
async def handle_device_regenerate_do(
    callback: CallbackQuery,
    database: Database,
    mediator_client: MediatorClient,
    settings: Settings,
) -> None:
    await callback.answer()
    await _deliver_shared_subscription_link(callback, database, mediator_client, settings)


@router.callback_query(F.data == "subscription:reset_confirm")
async def handle_reset_confirm(callback: CallbackQuery) -> None:
    await callback.answer()
    if callback.message is None:
        return
    await callback.message.edit_text(
        "<b>Запретить обновление подписки для всех устройств?</b>\n\n"
        "Все текущие ссылки перестанут обновляться. Затем нужные устройства "
        "придётся подключить заново. Уже импортированные вручную профили могут "
        "сохраниться в сторонних клиентах.",
        reply_markup=reset_confirm_keyboard(),
    )


@router.callback_query(F.data == "subscription:reset_do")
async def handle_reset_do(
    callback: CallbackQuery,
    database: Database,
    settings: Settings,
    mediator_client: MediatorClient,
) -> None:
    await callback.answer()
    if callback.from_user is None or callback.message is None:
        return

    async with database.session() as session:
        user = await UserRepository(session).get_by_telegram_id(callback.from_user.id)
        subscription = (
            await SubscriptionRepository(session).get_active_for_user(user.id)
            if user is not None
            else None
        )
        if user is None or subscription is None:
            await callback.message.edit_text(
                "Действующий доступ не найден.",
                reply_markup=back_to_main_keyboard(),
            )
            return
        can_reset, next_allowed_at = await DeviceResetRepository(session).can_reset(
            subscription_id=subscription.id,
            cooldown_hours=settings.device_reset_cooldown_hours,
        )
        user_id = user.id
        subscription_id = subscription.id
        subscription_guid = subscription.public_guid

    if not can_reset:
        next_text = (
            format_datetime_ru(to_aware_utc(next_allowed_at))
            if next_allowed_at is not None
            else "позже"
        )
        await callback.message.edit_text(
            f"Повторный сброс будет доступен {next_text}.\n\nВаш VPN продолжает работать.",
            reply_markup=subscription_keyboard(True),
        )
        return

    try:
        unbound_devices = await mediator_client.revoke_all_device_tokens(subscription_guid)
    except MediatorClientError as exception:
        _log_mediator_error("reset_devices", callback, exception)
        await _show_service_unavailable(callback)
        return

    async with database.session() as session:
        user = await UserRepository(session).get_by_telegram_id(callback.from_user.id)
        subscription = await SubscriptionRepository(session).get_by_id(subscription_id)
        if user is not None and subscription is not None and user.id == user_id:
            await DeviceResetRepository(session).add_reset_event(subscription, user)
            await OnboardingSessionRepository(
                session
            ).restart_open_device_issuance_for_subscription(
                user.id,
                subscription.id,
            )

    await callback.message.edit_text(
        "<b>Устройства сброшены</b>\n\n"
        f"Отвязано устройств: <b>{unbound_devices}</b>\n\n"
        "Теперь подключите нужные устройства заново.",
        reply_markup=subscription_keyboard(True),
    )
