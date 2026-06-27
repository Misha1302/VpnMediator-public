from __future__ import annotations

import json
import logging

from aiogram import Router
from aiogram.enums import ChatType
from aiogram.filters import BaseFilter, Command
from aiogram.types import Message
from sqlalchemy import select

from vpn_access_bot.advertising_readiness import (
    AcquisitionService,
    CapacityService,
    CommerceOperationKind,
    CommercePolicyChangeError,
    CommercePolicyRepository,
)
from vpn_access_bot.broadcast import (
    BroadcastCommandError,
    BroadcastService,
    parse_broadcast_command,
    parse_broadcast_confirmation,
)
from vpn_access_bot.config import Settings
from vpn_access_bot.constants import (
    ORDER_STATUS_ACTIVATION_FAILED,
    ORDER_STATUS_PENDING,
)
from vpn_access_bot.correlation import get_correlation_id
from vpn_access_bot.db import Database
from vpn_access_bot.formatting import escape_html
from vpn_access_bot.keyboards import after_purchase_keyboard, format_price
from vpn_access_bot.mediator_client import MediatorClient, MediatorClientError
from vpn_access_bot.models import AcquisitionCampaign, Order, Subscription
from vpn_access_bot.readiness import CommerceReadinessService
from vpn_access_bot.repositories import (
    AuditRepository,
    EntitlementOperationRepository,
    EntitlementRepository,
    OrderApplicationRepository,
    OrderRepository,
    SubscriptionRepository,
    to_aware_utc,
)
from vpn_access_bot.services import (
    AdminEntitlementAdjustmentService,
    ExpirationService,
    PurchaseService,
    ReconciliationRepairService,
)
from vpn_access_bot.telegram import BotRegistry
from vpn_access_bot.test_user_reset import TestUserResetService
from vpn_access_bot.texts import format_datetime

router = Router(name="admin")
logger = logging.getLogger(__name__)


def is_admin(message: Message, settings: Settings) -> bool:
    return message.from_user is not None and message.from_user.id in settings.admin_telegram_ids


class AdminMessageFilter(BaseFilter):
    async def __call__(self, message: Message, settings: Settings) -> bool:
        return is_admin(message, settings)


router.message.filter(AdminMessageFilter())


def parse_order_id(message: Message) -> int | None:
    parts = (message.text or "").split()

    if len(parts) != 2 or not parts[1].isdigit():
        return None

    return int(parts[1])


def user_label(order: Order) -> str:
    username = f"@{order.user.username}" if order.user.username else "без username"
    return f"{order.user.telegram_id} ({username})"


def order_product_label(order: Order) -> str:
    if order.tariff is not None:
        return order.tariff.title

    labels = {
        "purchase": "новый доступ",
        "extend": "продление",
        "resume": "возобновление",
        "upgrade_devices": "увеличение числа устройств",
    }
    label = labels.get(order.order_kind, "операция с доступом")
    if order.order_kind == "upgrade_devices":
        return f"{label}: до {order.selected_max_devices} устройств"
    return f"{label}: {order.period_count} мес., {order.selected_max_devices} устройств"


def order_summary_line(order: Order) -> str:
    return (
        f"<code>{order.id}</code> | {user_label(order)} | "
        f"{order_product_label(order)} | "
        f"{format_price(order.amount_minor_units, order.currency)} | "
        f"{format_datetime(order.created_at)}"
    )


def _order_status_label(status: str) -> str:
    return {
        "pending": "ожидает оплаты",
        "payment_received": "оплата получена",
        "activating": "активируется",
        "paid": "активирован",
        "activation_failed": "ошибка активации",
        "refunded": "возвращён",
        "expired": "истёк",
        "failed": "ошибка",
    }.get(status, status)


_RECONCILIATION_REJECTION_TEXTS = {
    "subscription_not_found": "Подписка не найдена.",
    "subscription_is_not_quarantined": "Подписка не находится в quarantine.",
    "reconciliation_expected_remote_version_required": (
        "Нужно явно указать ожидаемую версию состояния Mediator."
    ),
    "reconciliation_snapshot_changed": (
        "Состояние Mediator изменилось. Сначала повторите /reconcile_status."
    ),
    "reconciliation_active_operation_exists": (
        "Для подписки уже выполняется другая entitlement-операция."
    ),
    "reconciliation_unfinished_order_exists": (
        "У подписки есть незавершённый заказ. Сначала завершите или разберите его."
    ),
    "reconciliation_remote_status_not_disabled": (
        "Удалённое состояние не является принудительно отключённым."
    ),
    "ambiguous_disabled_entitlement_origin": (
        "Статус disabled неоднозначен. Используйте явную команду adopt_expired "
        "или adopt_disabled после проверки состояния."
    ),
    "legacy_expiration_requires_expired_subscription": (
        "Legacy expiration можно принять только для уже истёкшей подписки."
    ),
    "legacy_expiration_has_future_validity": (
        "Срок подписки ещё не истёк; принимать состояние как expiration нельзя."
    ),
    "legacy_expiration_snapshot_mismatch": (
        "Локальные и удалённые параметры legacy expiration не совпадают."
    ),
    "access_operation_in_progress": "Для пользователя уже выполняется операция доступа.",
}


def _safe_reconciliation_rejection_text(exception: ValueError) -> tuple[str, str]:
    candidate = exception.args[0] if len(exception.args) == 1 else None
    code = candidate if isinstance(candidate, str) else ""
    safe_code = code if code in _RECONCILIATION_REJECTION_TEXTS else "unknown"
    text = _RECONCILIATION_REJECTION_TEXTS.get(
        safe_code,
        "Операция отклонена проверками безопасности. Повторите /reconcile_status.",
    )
    return safe_code, text


def _admin_failure_text(action: str) -> str:
    correlation_id = get_correlation_id() or "не указан"
    return (
        f"Не удалось выполнить действие: <b>{action}</b>.\n"
        f"Код обращения: <code>{correlation_id}</code>"
    )


def _broadcast_usage(*, filtered: bool) -> str:
    if filtered:
        return (
            "Формат:\n"
            "<code>/broadcast_regex REGEX</code>\n"
            "текст сообщения с новой строки\n\n"
            "Regex применяется ко всей строке Telegram ID. Пример: "
            "<code>^123\\d+$</code>."
        )
    return "Формат:\n<code>/broadcast</code>\nтекст сообщения с новой строки"


def _broadcast_error_text(error: BroadcastCommandError, *, filtered: bool) -> str:
    code = str(error)
    details = {
        "message_required": "После первой строки команды нужен непустой текст рассылки.",
        "message_too_long": "Сообщение длиннее лимита Telegram в 4096 символов.",
        "message_contains_nul": "Сообщение содержит недопустимый нулевой символ.",
        "invalid_regex": "Regex пустой, слишком длинный или синтаксически неверный.",
        "unsafe_regex": (
            "Regex содержит потенциально опасную конструкцию. "
            "Используйте выражение без lookaround, backreference и квантификаторов групп."
        ),
        "invalid_command": "Первая строка команды заполнена неверно.",
        "campaign_already_exists": "Эта команда уже была обработана.",
        "campaign_already_queued": "Эта рассылка уже подтверждена и поставлена в очередь.",
    }.get(code, "Команда рассылки заполнена неверно.")
    return f"{details}\n\n{_broadcast_usage(filtered=filtered)}"


def _broadcast_confirmation_error_text(error: BroadcastCommandError) -> str:
    code = str(error)
    return {
        "confirmation_token_required": (
            "Формат: <code>/broadcast_confirm TOKEN</code>. Токен указан в сообщении предпросмотра."
        ),
        "confirmation_token_invalid": "Токен подтверждения неверен.",
        "confirmation_token_expired": (
            "Срок подтверждения истёк. Создайте новую рассылку командой /broadcast."
        ),
        "campaign_has_no_recipients": "В кампании нет получателей.",
        "campaign_still_preparing": (
            "Кампания ещё подготавливается. Повторите через несколько секунд."
        ),
        "campaign_not_confirmable": "Эту кампанию больше нельзя подтвердить.",
    }.get(code, "Не удалось подтвердить рассылку.")


async def _handle_broadcast(
    message: Message,
    database: Database,
    settings: Settings,
    *,
    filtered: bool,
) -> None:
    if (
        not is_admin(message, settings)
        or message.from_user is None
        or message.chat.type != ChatType.PRIVATE
    ):
        return

    try:
        request = parse_broadcast_command(message.text, filtered=filtered)
    except BroadcastCommandError as exception:
        await message.answer(_broadcast_error_text(exception, filtered=filtered))
        return

    try:
        preview = await BroadcastService(database.session).prepare(
            request,
            admin_telegram_id=message.from_user.id,
            source_chat_id=message.chat.id,
            source_message_id=message.message_id,
            confirmation_ttl_minutes=settings.broadcast_confirmation_ttl_minutes,
        )
    except BroadcastCommandError as exception:
        await message.answer(_broadcast_error_text(exception, filtered=filtered))
        return
    except Exception:
        logger.exception("Failed to prepare admin broadcast.")
        await message.answer(_admin_failure_text("подготовка рассылки"))
        return

    if preview is None:
        await message.answer(
            "Получатели не найдены. Рассылка не создана."
            if filtered
            else "В базе пока нет пользователей для рассылки."
        )
        return

    await message.answer(
        "⚠️ <b>Проверьте рассылку перед отправкой</b>\n\n"
        f"Получателей: <b>{preview.target_count}</b>\n"
        f"Размер сообщения: <b>{preview.message_length}</b> символов\n"
        f"SHA-256: <code>{preview.message_sha256}</code>\n"
        f"Кампания: <code>{escape_html(preview.campaign_id)}</code>\n\n"
        "Для отправки выполните отдельную команду:\n"
        f"<code>/broadcast_confirm {escape_html(preview.confirmation_token)}</code>\n\n"
        "Токен одноразовый, привязан к этому администратору и личному чату и действует "
        f"до <code>{preview.expires_at_utc.isoformat()}</code>."
    )


@router.message(Command("broadcast"))
async def handle_broadcast(
    message: Message,
    database: Database,
    settings: Settings,
) -> None:
    await _handle_broadcast(message, database, settings, filtered=False)


@router.message(Command("broadcast_regex"))
async def handle_broadcast_regex(
    message: Message,
    database: Database,
    settings: Settings,
) -> None:
    await _handle_broadcast(message, database, settings, filtered=True)


@router.message(Command("broadcast_confirm"))
async def handle_broadcast_confirm(
    message: Message,
    database: Database,
    settings: Settings,
) -> None:
    if (
        not is_admin(message, settings)
        or message.from_user is None
        or message.chat.type != ChatType.PRIVATE
    ):
        return
    try:
        token = parse_broadcast_confirmation(message.text)
        result = await BroadcastService(database.session).confirm_and_enqueue(
            token,
            admin_telegram_id=message.from_user.id,
            source_chat_id=message.chat.id,
        )
    except BroadcastCommandError as exception:
        await message.answer(_broadcast_confirmation_error_text(exception))
        return
    except Exception:
        logger.exception("Failed to confirm admin broadcast.")
        await message.answer(_admin_failure_text("подтверждение рассылки"))
        return

    await message.answer(
        "✅ <b>Рассылка поставлена в очередь</b>\n\n"
        f"Получателей: <b>{result.target_count}</b>\n"
        f"Новых сообщений в очереди: <b>{result.newly_queued_count}</b>\n"
        f"Уже были поставлены ранее: <b>{result.already_queued_count}</b>\n"
        f"Кампания: <code>{escape_html(result.campaign_id)}</code>"
    )


def order_details_text(order: Order, subscription: Subscription | None) -> str:
    subscription_lines = ["Связанная подписка: <b>нет</b>"]

    if subscription is not None:
        subscription_lines = [
            "Связанная подписка: <b>есть</b>",
            f"Public GUID: <code>{subscription.public_guid}</code>",
            f"Действует до: <b>{format_datetime(subscription.expires_at)}</b>",
        ]

    paid_at = format_datetime(order.paid_at) if order.paid_at is not None else "не оплачен"
    payment_reference = "есть" if order.provider_payment_id else "нет"

    return (
        "<b>Данные заказа</b>\n\n"
        f"Номер: <code>{order.id}</code>\n"
        f"Статус: <b>{_order_status_label(order.status)}</b>\n"
        f"Пользователь: <code>{order.user.telegram_id}</code>\n"
        f"Username: <b>{order.user.username or 'не указан'}</b>\n"
        f"Операция: <b>{order_product_label(order)}</b>\n"
        f"Сумма: <b>{format_price(order.amount_minor_units, order.currency)}</b>\n"
        f"Провайдер: <b>{order.provider}</b>\n"
        f"Платёжный идентификатор: <b>{payment_reference}</b>\n"
        f"Создан: <b>{format_datetime(order.created_at)}</b>\n"
        f"Оплачен: <b>{paid_at}</b>\n\n" + "\n".join(subscription_lines)
    )


async def notify_user_subscription_active(message: Message, subscription: Subscription) -> None:
    await message.bot.send_message(
        chat_id=subscription.user.telegram_id,
        text=(
            "✅ <b>Подписка активирована</b>\n\n"
            f"Действует до: <b>{format_datetime(subscription.expires_at)}</b>\n"
            f"Устройств: <b>{subscription.max_devices}</b>\n\n"
            "Теперь можно подключить устройство через кнопку ниже."
        ),
        reply_markup=after_purchase_keyboard(),
    )


@router.message(Command("admin"))
async def handle_admin(
    message: Message,
    database: Database,
    settings: Settings,
) -> None:
    if not is_admin(message, settings):
        return

    async with database.session() as session:
        active_count = await SubscriptionRepository(session).count_active()

    await message.answer(
        "<b>Администрирование</b>\n\n"
        f"Активных подписок: <b>{active_count}</b>\n\n"
        "Команды:\n"
        "<code>/broadcast</code> + текст с новой строки\n"
        "<code>/broadcast_regex REGEX</code> + текст с новой строки\n"
        "<code>/broadcast_confirm TOKEN</code>\n"
        "<code>/approve_order ORDER_ID</code>\n"
        "<code>/pending_orders</code>\n"
        "<code>/failed_orders</code>\n"
        "<code>/order ORDER_ID</code>\n"
        "<code>/retry_order ORDER_ID</code>\n"
        "<code>/refund_order ORDER_ID</code>\n"
        "<code>/sync_expired</code>\n"
        "<code>/reconcile_status GUID</code>\n"
        "<code>/reconcile_adopt_remote GUID REASON</code>\n"
        "<code>/reconcile_adopt_expired GUID VERSION REASON</code>\n"
        "<code>/reconcile_adopt_disabled GUID VERSION REASON</code>\n"
        "<code>/reconcile_restore_local GUID REASON</code>\n"
        "<code>/adjust_subscription GUID DAYS DEVICE_LIMIT REASON</code>\n"
        "<code>/revoke_subscription GUID REASON</code>\n"
        "<code>/discount_set TELEGRAM_ID PERCENT ...</code>\n"
        "<code>/discount_show TELEGRAM_ID</code>\n"
        "<code>/discount_remove TELEGRAM_ID</code>\n"
        "<code>/test_user_reset TELEGRAM_ID CONFIRM_RESET_TELEGRAM_ID</code>\n"
        "<code>/referral_stats</code>\n"
        "<code>/support_close REQUEST_ID</code>\n"
        "<code>/workers</code>\n"
        "<code>/commerce_status</code>\n"
        "<code>/commerce_stop SWITCH REASON</code>\n"
        "<code>/commerce_start SWITCH REASON</code>\n"
        "<code>/capacity_status</code>\n"
        "<code>/campaign_create CHANNEL [PLACEMENT] [CREATIVE]</code>\n"
        "<code>/campaign_status TOKEN</code>\n"
        "<code>/product_funnel [DAYS]</code>",
    )


@router.message(Command("commerce_status"))
async def handle_commerce_status(
    message: Message,
    settings: Settings,
    readiness_service: CommerceReadinessService,
) -> None:
    if not is_admin(message, settings):
        return
    decisions = await readiness_service.all_decisions(force=True)
    lines = ["<b>Commerce admission</b>"]
    for decision in decisions:
        state = "разрешено" if decision.allowed else "запрещено"
        lines.append(
            f"<code>{decision.operation_kind.value}</code>: <b>{state}</b> "
            f"(<code>{escape_html(decision.reason_code)}</code>)"
        )
    await message.answer("\n".join(lines))


async def _prepare_commerce_switch_change(
    message: Message,
    database: Database,
    settings: Settings,
    *,
    enabled: bool,
) -> None:
    if not is_admin(message, settings) or message.from_user is None:
        return
    parts = (message.text or "").split(maxsplit=2)
    if len(parts) < 3:
        command = "commerce_start" if enabled else "commerce_stop"
        await message.answer(f"Формат: <code>/{command} SWITCH REASON</code>")
        return
    switch_name, reason = parts[1], parts[2].strip()
    try:
        async with database.session() as session:
            repository = CommercePolicyRepository(session)
            preview = await repository.prepare_switch_change(
                switch_name=switch_name,
                enabled=enabled,
                admin_telegram_id=message.from_user.id,
                reason_code=reason,
                operator_note=reason,
            )
            await AuditRepository(session).add(
                event_type="commerce_policy_change_prepared",
                telegram_id=message.from_user.id,
                details_json=json.dumps(
                    {
                        "request_public_id": preview.public_id,
                        "switch": switch_name,
                        "enabled": enabled,
                        "expected_policy_version": preview.expected_policy_version,
                        "reason_code": reason[:64],
                    },
                    ensure_ascii=False,
                    sort_keys=True,
                ),
            )
            await session.commit()
    except CommercePolicyChangeError as error:
        if str(error) == "unknown_commerce_switch":
            await message.answer(
                "Неизвестный switch. Допустимы: new_purchases, trials, renewals, resumes, "
                "device_upgrades, extend_and_upgrade, referrals, campaign_tracking, "
                "capacity_enforcement."
            )
            return
        raise
    requested_state = "включить" if enabled else "выключить"
    await message.answer(
        "<b>Подтверждение изменения commerce policy</b>\n"
        f"Действие: <b>{requested_state}</b> <code>{escape_html(switch_name)}</code>\n"
        f"Ожидаемая версия policy: <code>{preview.expected_policy_version}</code>\n"
        f"Причина: {escape_html(reason[:256])}\n\n"
        f"<code>/confirm_commerce {escape_html(preview.confirmation_token)}</code>\n"
        "Токен одноразовый и действует 5 минут."
    )


@router.message(Command("commerce_stop"))
async def handle_commerce_stop(
    message: Message,
    database: Database,
    settings: Settings,
) -> None:
    await _prepare_commerce_switch_change(message, database, settings, enabled=False)


@router.message(Command("commerce_start"))
async def handle_commerce_start(
    message: Message,
    database: Database,
    settings: Settings,
) -> None:
    await _prepare_commerce_switch_change(message, database, settings, enabled=True)


@router.message(Command("confirm_commerce"))
async def handle_confirm_commerce(
    message: Message,
    database: Database,
    settings: Settings,
) -> None:
    if not is_admin(message, settings) or message.from_user is None:
        return
    parts = (message.text or "").split(maxsplit=1)
    if len(parts) != 2 or not parts[1].strip():
        await message.answer("Формат: <code>/confirm_commerce TOKEN</code>")
        return
    token = parts[1].strip()
    try:
        async with database.session() as session:
            repository = CommercePolicyRepository(session)
            try:
                policy, request = await repository.confirm_switch_change(
                    confirmation_token=token,
                    admin_telegram_id=message.from_user.id,
                )
            except CommercePolicyChangeError:
                await session.commit()
                raise
            await AuditRepository(session).add(
                event_type="commerce_policy_changed",
                telegram_id=message.from_user.id,
                details_json=json.dumps(
                    {
                        "request_public_id": request.public_id,
                        "switch": request.switch_name,
                        "enabled": request.requested_enabled,
                        "policy_version": policy.version,
                        "reason_code": request.reason_code,
                    },
                    ensure_ascii=False,
                    sort_keys=True,
                ),
            )
            await session.commit()
    except CommercePolicyChangeError as error:
        messages = {
            "commerce_confirmation_invalid": "Токен подтверждения неверен.",
            "commerce_confirmation_already_used": "Токен уже использован или отменён.",
            "commerce_confirmation_expired": "Токен подтверждения истёк. Повторите команду.",
            "commerce_policy_version_conflict": (
                "Commerce policy уже изменилась. Старый токен отменён; сформируйте новый preview."
            ),
        }
        await message.answer(messages.get(str(error), "Изменение commerce policy не применено."))
        return
    await message.answer(
        f"Commerce switch <code>{escape_html(request.switch_name)}</code>: "
        f"<b>{'включён' if request.requested_enabled else 'выключен'}</b>. "
        f"Policy version: <code>{policy.version}</code>."
    )


@router.message(Command("capacity_status"))
async def handle_capacity_status(
    message: Message,
    database: Database,
    settings: Settings,
    mediator_client: MediatorClient,
) -> None:
    if not is_admin(message, settings):
        return
    try:
        mediator = await mediator_client.get_readiness()
    except MediatorClientError:
        mediator = None
    async with database.session() as session:
        snapshot = await CapacityService(session, settings).capture(mediator)
    device_count = snapshot.active_devices if snapshot.active_devices is not None else "неизвестно"
    utilization = (
        f"{snapshot.utilization_percent:.1f}%"
        if snapshot.utilization_percent is not None
        else "неизвестно"
    )
    await message.answer(
        "<b>Capacity snapshot</b>\n"
        f"Состояние: <code>{snapshot.state}</code> "
        f"(<code>{snapshot.reason_code}</code>)\n"
        f"Активных подписок: <b>{snapshot.active_subscriptions}</b>\n"
        f"Устройств: <b>{device_count}</b>\n"
        f"Utilization: <b>{utilization}</b>\n"
        f"Payment backlog: <b>{snapshot.payment_inbox_pending}</b>\n"
        f"Activation backlog: <b>{snapshot.activation_pending}</b>\n"
        f"Refund pending/manual: <b>{snapshot.refund_pending}/{snapshot.refund_manual_review}</b>\n"
        f"Stale workers: <b>{snapshot.worker_stale_count}</b>"
    )


@router.message(Command("campaign_create"))
async def handle_campaign_create(
    message: Message,
    database: Database,
    settings: Settings,
) -> None:
    if not is_admin(message, settings):
        return
    parts = (message.text or "").split(maxsplit=3)
    if len(parts) < 2:
        await message.answer("Формат: <code>/campaign_create CHANNEL [PLACEMENT] [CREATIVE]</code>")
        return
    async with database.session() as session:
        policy = await CommercePolicyRepository(session).get()
        if not policy.campaign_tracking_enabled:
            await message.answer("Campaign tracking выключен commerce policy.")
            return
        campaign = await AcquisitionService(session).create_campaign(
            channel=parts[1],
            placement=parts[2] if len(parts) >= 3 else None,
            creative=parts[3] if len(parts) >= 4 else None,
        )
        await session.commit()
    link = AcquisitionService.campaign_deep_link(
        settings.public_telegram_bot_username,
        campaign,
    )
    await message.answer(
        "<b>Кампания создана</b>\n"
        f"Token: <code>{campaign.public_token}</code>\n"
        f"Channel: <code>{escape_html(campaign.channel)}</code>\n"
        f"Deep link: <code>{escape_html(link)}</code>"
    )


@router.message(Command("campaign_status"))
async def handle_campaign_status(
    message: Message,
    database: Database,
    settings: Settings,
) -> None:
    if not is_admin(message, settings):
        return
    parts = (message.text or "").split(maxsplit=1)
    if len(parts) != 2:
        await message.answer("Формат: <code>/campaign_status TOKEN</code>")
        return
    token = parts[1].removeprefix("c_").strip()
    async with database.session() as session:
        result = await session.execute(
            select(AcquisitionCampaign).where(AcquisitionCampaign.public_token == token)
        )
        campaign = result.scalar_one_or_none()
    if campaign is None:
        await message.answer("Кампания не найдена.")
        return
    await message.answer(
        "<b>Campaign</b>\n"
        f"Token: <code>{campaign.public_token}</code>\n"
        f"Status: <code>{campaign.status}</code>\n"
        f"Channel: <code>{escape_html(campaign.channel)}</code>\n"
        f"Placement: <code>{escape_html(campaign.placement or '—')}</code>\n"
        f"Creative: <code>{escape_html(campaign.creative or '—')}</code>"
    )


@router.message(Command("pending_orders"))
async def handle_pending_orders(
    message: Message,
    database: Database,
    settings: Settings,
) -> None:
    if not is_admin(message, settings):
        return

    async with database.session() as session:
        orders = await OrderRepository(session).list_by_status(ORDER_STATUS_PENDING)

    if not orders:
        await message.answer("Нет заказов, ожидающих оплаты.")
        return

    await message.answer(
        "<b>Заказы, ожидающие оплаты</b>\n\n"
        + "\n".join(order_summary_line(order) for order in orders),
    )


@router.message(Command("failed_orders"))
async def handle_failed_orders(
    message: Message,
    database: Database,
    settings: Settings,
) -> None:
    if not is_admin(message, settings):
        return

    async with database.session() as session:
        orders = await OrderRepository(session).list_by_status(ORDER_STATUS_ACTIVATION_FAILED)

    if not orders:
        await message.answer("Нет заказов с ошибкой активации.")
        return

    await message.answer(
        "<b>Заказы с ошибкой активации</b>\n\n"
        + "\n".join(order_summary_line(order) for order in orders),
    )


@router.message(Command("order"))
async def handle_order_details(
    message: Message,
    database: Database,
    settings: Settings,
) -> None:
    if not is_admin(message, settings):
        return

    order_id = parse_order_id(message)

    if order_id is None:
        await message.answer("Формат: <code>/order ORDER_ID</code>")
        return

    async with database.session() as session:
        order = await OrderRepository(session).get_by_id(order_id)

        if order is None:
            await message.answer("Заказ не найден.")
            return

        subscription_repository = SubscriptionRepository(session)
        application = await OrderApplicationRepository(session).get_for_order(order.id)
        subscription = None

        if application is not None:
            subscription = await subscription_repository.get_by_id(application.subscription_id)
        elif order.target_subscription_id is not None:
            subscription = await subscription_repository.get_by_id(order.target_subscription_id)

    await message.answer(order_details_text(order, subscription))


@router.message(Command("approve_order"))
async def handle_approve_order(
    message: Message,
    database: Database,
    settings: Settings,
    mediator_client: MediatorClient,
    readiness_service: CommerceReadinessService,
) -> None:
    if not is_admin(message, settings):
        return

    order_id = parse_order_id(message)

    if order_id is None:
        await message.answer("Формат: <code>/approve_order ORDER_ID</code>")
        return

    readiness = await readiness_service.check(
        operation_kind=CommerceOperationKind.COMPLETE_PAID_ORDER,
        force=True,
    )
    if not readiness.can_sell:
        await message.answer(
            "Новый доступ сейчас нельзя выдать. Оплата не должна подтверждаться до "
            "восстановления готовности mediator."
        )
        return

    try:
        async with database.session() as session:
            service = PurchaseService(session, settings, mediator_client)
            payment = await service.prepare_manual_order_for_activation(
                order_id,
                admin_telegram_id=message.from_user.id,
            )
    except Exception:
        logger.exception("Failed to approve order. Order id: %s", order_id)
        await message.answer(_admin_failure_text("подтверждение заказа"))
        return

    if payment.already_paid and payment.subscription is not None:
        await message.answer(
            "Заказ уже оплачен и активирован.\n\n"
            f"Подписка: <code>{payment.subscription.public_guid}</code>\n"
            f"Действует до: <b>{format_datetime(payment.subscription.expires_at)}</b>",
        )
        return

    async with database.session() as session:
        service = PurchaseService(session, settings, mediator_client)
        activation = await service.activate_order_by_id(payment.order_id)

    if activation.failure_message is not None or activation.subscription is None:
        await message.answer(
            "Оплата записана, но доступ пока не активирован.\n\n"
            f"Заказ: <code>{payment.order_id}</code>\n"
            "Проверьте готовность сервиса и выполните <code>/retry_order ORDER_ID</code>.",
        )
        return

    subscription = activation.subscription

    await message.answer(
        "✅ <b>Заказ подтверждён</b>\n\n"
        f"Подписка: <code>{subscription.public_guid}</code>\n"
        f"Действует до: <b>{format_datetime(subscription.expires_at)}</b>",
    )

    try:
        await notify_user_subscription_active(message, subscription)
    except Exception:
        logger.exception(
            "Failed to notify user after order activation. Order id: %s",
            order_id,
        )
        await message.answer("Подписка активна, но уведомление пользователю не отправлено.")


@router.message(Command("retry_order"))
async def handle_retry_order(
    message: Message,
    database: Database,
    settings: Settings,
    mediator_client: MediatorClient,
    readiness_service: CommerceReadinessService,
) -> None:
    if not is_admin(message, settings):
        return

    order_id = parse_order_id(message)

    if order_id is None:
        await message.answer("Формат: <code>/retry_order ORDER_ID</code>")
        return

    readiness = await readiness_service.check(
        operation_kind=CommerceOperationKind.RETRY_ACTIVATION,
        force=True,
    )
    if not readiness.can_sell:
        await message.answer(
            "Повторная активация пока невозможна: mediator не готов выдать рабочий доступ."
        )
        return

    try:
        async with database.session() as session:
            service = PurchaseService(session, settings, mediator_client)
            activation = await service.retry_activation_by_id(order_id)
    except Exception:
        logger.exception("Failed to retry order. Order id: %s", order_id)
        await message.answer(_admin_failure_text("повторная активация заказа"))
        return

    if activation.failure_message is not None or activation.subscription is None:
        await message.answer(
            "Активация снова не выполнена.\n\n"
            f"Заказ: <code>{order_id}</code>\n"
            "Проверьте готовность сервиса и повторите попытку позже.",
        )
        return

    subscription = activation.subscription

    if activation.already_paid:
        await message.answer(
            "Заказ уже оплачен и активирован.\n\n"
            f"Подписка: <code>{subscription.public_guid}</code>\n"
            f"Действует до: <b>{format_datetime(subscription.expires_at)}</b>",
        )
        return

    await message.answer(
        "✅ <b>Заказ активирован</b>\n\n"
        f"Подписка: <code>{subscription.public_guid}</code>\n"
        f"Действует до: <b>{format_datetime(subscription.expires_at)}</b>",
    )

    try:
        await notify_user_subscription_active(message, subscription)
    except Exception:
        logger.exception(
            "Failed to notify user after order activation. Order id: %s",
            order_id,
        )
        await message.answer("Подписка активна, но уведомление пользователю не отправлено.")


@router.message(Command("refund_order"))
async def handle_refund_order(
    message: Message,
    database: Database,
    settings: Settings,
    mediator_client: MediatorClient,
) -> None:
    if not is_admin(message, settings) or message.from_user is None:
        return

    order_id = parse_order_id(message)
    if order_id is None:
        await message.answer("Формат: <code>/refund_order ORDER_ID</code>")
        return

    try:
        async with database.session() as session:
            candidate = await PurchaseService(session, settings, mediator_client).preview_refund(
                order_id,
                admin_telegram_id=message.from_user.id,
            )
    except Exception:
        logger.exception("Failed to prepare refund preview. Order id: %s", order_id)
        await message.answer(_admin_failure_text("подготовка плана возврата"))
        return

    if candidate.already_refunded:
        await message.answer("Возврат по заказу уже выполнен.")
        return
    if not candidate.is_eligible or candidate.confirmation_token is None:
        await message.answer(candidate.reason or "Заказ не подходит для автоматического возврата.")
        return

    target_until = (
        candidate.target_valid_until_utc.isoformat()
        if candidate.target_valid_until_utc is not None
        else "без технической подписки"
    )
    referral_line = {
        "cancel": "ожидающая награда будет отменена",
        "reverse": "применённая награда будет поставлена на durable reversal",
    }.get(candidate.referral_action, "реферальной награды нет")
    await message.answer(
        "<b>Предпросмотр возврата</b>\n"
        f"Заказ: <code>{order_id}</code>\n"
        f"Тип: <code>{candidate.order.order_kind}</code>\n"
        f"Сумма: <code>{candidate.order.amount_minor_units} "
        f"{candidate.order.currency}</code>\n"
        f"После возврата: <code>{candidate.target_status}</code>, "
        f"до <code>{target_until}</code>, "
        f"устройств <code>{candidate.target_max_devices or '—'}</code>\n"
        f"Реферальный lifecycle: {referral_line}.\n\n"
        "Telegram refund ещё <b>не вызван</b>. Для подтверждения:\n"
        f"<code>/confirm_refund {candidate.confirmation_token}</code>\n"
        f"Токен действует {max(settings.refund_confirmation_ttl_minutes, 1)} минут."
    )


@router.message(Command("confirm_refund"))
async def handle_confirm_refund(
    message: Message,
    database: Database,
    settings: Settings,
    mediator_client: MediatorClient,
    bot_registry: BotRegistry,
) -> None:
    if not is_admin(message, settings) or message.from_user is None:
        return
    parts = (message.text or "").split(maxsplit=1)
    if len(parts) != 2 or not parts[1].strip():
        await message.answer("Формат: <code>/confirm_refund TOKEN</code>")
        return
    token = parts[1].strip()

    try:
        async with database.session() as session:
            candidate = await PurchaseService(session, settings, mediator_client).confirm_refund(
                token,
                admin_telegram_id=message.from_user.id,
            )
    except ValueError as exception:
        error_code = exception.args[0] if exception.args else None
        reason = {
            "refund_confirmation_invalid": "токен недействителен или уже использован",
            "refund_confirmation_already_used": "план уже был подтверждён",
            "refund_confirmation_expired": "срок действия токена истёк",
            "refund_confirmation_actor_mismatch": "токен создан другим администратором",
            "access_operation_in_progress": "для пользователя уже выполняется операция доступа",
            "refund_state_changed": (
                "состояние подписки изменилось; provider refund не вызывался, "
                "требуется новый preview или ручная проверка"
            ),
            "refund_operation_not_found": "durable refund operation не найдена",
        }.get(error_code, "план больше нельзя безопасно подтвердить")
        await message.answer(f"Возврат не подтверждён: {reason}.")
        return
    except Exception:
        logger.exception("Failed to confirm refund token")
        await message.answer(_admin_failure_text("подтверждение возврата"))
        return

    if candidate.charge_id is None:
        await message.answer("В подтверждённом плане отсутствует provider charge id.")
        return

    refund_bot = message.bot
    refund_bot_key = candidate.order.payment_bot_key or candidate.order.origin_bot_key
    if refund_bot_key is not None:
        payment_runtime = bot_registry.get(refund_bot_key)
        if payment_runtime is None or payment_runtime.bot is None:
            await message.answer(
                "Нельзя выполнить возврат: Telegram-бот, принявший оплату, недоступен. "
                "Операция сохранена как refunding; восстановите исходного бота."
            )
            return
        refund_bot = payment_runtime.bot

    try:
        await refund_bot.refund_star_payment(
            user_id=candidate.order.user.telegram_id,
            telegram_payment_charge_id=candidate.charge_id,
        )
    except Exception:
        logger.exception(
            "Telegram Stars refund outcome is unknown. Order id: %s",
            candidate.order.id,
        )
        async with database.session() as session:
            await PurchaseService(session, settings, mediator_client).mark_refund_provider_unknown(
                candidate.order.id, "telegram_refund_outcome_unknown"
            )
        await message.answer(
            "Telegram не подтвердил результат возврата. Повторный provider call автоматически "
            "не запускается: операция переведена на ручную проверку."
        )
        return

    try:
        async with database.session() as session:
            await PurchaseService(
                session, settings, mediator_client
            ).complete_refund_after_provider(candidate.order.id)
    except Exception:
        logger.exception(
            "Refund compensation/finalization failed. Order id: %s",
            candidate.order.id,
        )
        await message.answer(
            "Telegram подтвердил возврат, но exact compensation ещё не завершена. "
            "Provider success сохранён; recovery worker продолжит операцию без второго возврата."
        )
        return

    await message.answer(
        f"✅ По заказу <code>{candidate.order.id}</code> выполнен возврат Telegram Stars "
        "и применён сохранённый RefundPlan."
    )


@router.message(Command("adjust_subscription"))
async def handle_adjust_subscription(
    message: Message,
    database: Database,
    settings: Settings,
    mediator_client: MediatorClient,
) -> None:
    if not is_admin(message, settings) or message.from_user is None:
        return

    parts = (message.text or "").split(maxsplit=4)
    if len(parts) != 5:
        await message.answer(
            "Формат: <code>/adjust_subscription GUID DAYS DEVICE_LIMIT REASON</code>"
        )
        return
    public_guid, duration_text, limit_text, reason = parts[1:]
    try:
        duration_days = int(duration_text)
        device_limit = int(limit_text)
    except ValueError:
        await message.answer("DAYS и DEVICE_LIMIT должны быть целыми числами.")
        return

    try:
        async with database.session() as session:
            outcome = await AdminEntitlementAdjustmentService(session, mediator_client).apply(
                public_guid=public_guid,
                actor_telegram_id=message.from_user.id,
                source_request_id=f"telegram:{message.chat.id}:{message.message_id}",
                reason=reason,
                duration_days=duration_days,
                requested_device_limit=device_limit,
            )
    except (ValueError, RuntimeError):
        logger.exception("Admin entitlement adjustment failed.")
        await message.answer(_admin_failure_text("изменение подписки"))
        return

    await message.answer(
        "✅ Изменение применено.\n\n"
        f"Подписка: <code>{outcome.subscription.public_guid}</code>\n"
        f"Версия entitlement: <b>{outcome.version}</b>\n"
        f"Действует до: <b>{format_datetime(outcome.subscription.expires_at)}</b>\n"
        f"Устройств: <b>{outcome.subscription.max_devices}</b>"
    )


@router.message(Command("revoke_subscription"))
async def handle_revoke_subscription(
    message: Message,
    database: Database,
    settings: Settings,
    mediator_client: MediatorClient,
) -> None:
    if not is_admin(message, settings) or message.from_user is None:
        return

    parts = (message.text or "").split(maxsplit=2)
    if len(parts) != 3:
        await message.answer("Формат: <code>/revoke_subscription GUID REASON</code>")
        return
    public_guid, reason = parts[1:]
    try:
        async with database.session() as session:
            outcome = await AdminEntitlementAdjustmentService(session, mediator_client).apply(
                public_guid=public_guid,
                actor_telegram_id=message.from_user.id,
                source_request_id=f"telegram:{message.chat.id}:{message.message_id}",
                reason=reason,
                disable=True,
            )
    except (ValueError, RuntimeError):
        logger.exception("Admin entitlement revoke failed.")
        await message.answer(_admin_failure_text("отключение подписки"))
        return

    await message.answer(
        "✅ Доступ отключён.\n\n"
        f"Подписка: <code>{outcome.subscription.public_guid}</code>\n"
        f"Версия entitlement: <b>{outcome.version}</b>"
    )


async def _handle_reconciliation_repair(
    message: Message,
    database: Database,
    settings: Settings,
    mediator_client: MediatorClient,
    *,
    mode: str,
) -> None:
    if not is_admin(message, settings) or message.from_user is None:
        return

    expected_remote_version: int | None = None
    if mode in {"adopt_expired", "adopt_disabled"}:
        parts = (message.text or "").split(maxsplit=3)
        if len(parts) != 4 or not parts[2].isdigit():
            command = (
                "reconcile_adopt_expired" if mode == "adopt_expired" else "reconcile_adopt_disabled"
            )
            await message.answer(f"Формат: <code>/{command} GUID VERSION REASON</code>")
            return
        public_guid, version_text, reason = parts[1:]
        expected_remote_version = int(version_text)
    else:
        parts = (message.text or "").split(maxsplit=2)
        if len(parts) != 3:
            command = (
                "reconcile_adopt_remote" if mode == "adopt_remote" else "reconcile_restore_local"
            )
            await message.answer(f"Формат: <code>/{command} GUID REASON</code>")
            return
        public_guid, reason = parts[1:]

    try:
        async with database.session() as session:
            outcome = await ReconciliationRepairService(session, mediator_client).apply(
                public_guid=public_guid,
                actor_telegram_id=message.from_user.id,
                source_request_id=f"telegram:{message.chat.id}:{message.message_id}",
                reason=reason,
                mode=mode,
                expected_remote_version=expected_remote_version,
            )
    except ValueError as exception:
        safe_code, safe_text = _safe_reconciliation_rejection_text(exception)
        logger.warning(
            "Explicit reconciliation repair rejected. Mode: %s code=%s",
            mode,
            safe_code,
        )
        await message.answer(safe_text)
        return
    except RuntimeError:
        logger.exception("Explicit reconciliation repair failed. Mode: %s", mode)
        await message.answer(_admin_failure_text("восстановление согласованности"))
        return

    action = {
        "adopt_remote": "Принято однозначное состояние Mediator",
        "adopt_expired": "Legacy-состояние принято как естественно истёкшая подписка",
        "adopt_disabled": "Состояние Mediator принято как принудительное отключение",
        "restore_local": "Локальное подтверждённое состояние повторно применено через operation",
    }[outcome.mode]
    await message.answer(
        f"✅ {action}.\n\n"
        f"Подписка: <code>{outcome.subscription.public_guid}</code>\n"
        f"Бизнес-состояние: <b>{outcome.subscription.status}</b>\n"
        f"Entitlement: <b>{outcome.status}</b>, version <b>{outcome.version}</b>"
    )


@router.message(Command("reconcile_adopt_remote"))
async def handle_reconcile_adopt_remote(
    message: Message,
    database: Database,
    settings: Settings,
    mediator_client: MediatorClient,
) -> None:
    await _handle_reconciliation_repair(
        message,
        database,
        settings,
        mediator_client,
        mode="adopt_remote",
    )


@router.message(Command("reconcile_adopt_expired"))
async def handle_reconcile_adopt_expired(
    message: Message,
    database: Database,
    settings: Settings,
    mediator_client: MediatorClient,
) -> None:
    await _handle_reconciliation_repair(
        message,
        database,
        settings,
        mediator_client,
        mode="adopt_expired",
    )


@router.message(Command("reconcile_adopt_disabled"))
async def handle_reconcile_adopt_disabled(
    message: Message,
    database: Database,
    settings: Settings,
    mediator_client: MediatorClient,
) -> None:
    await _handle_reconciliation_repair(
        message,
        database,
        settings,
        mediator_client,
        mode="adopt_disabled",
    )


@router.message(Command("reconcile_status"))
async def handle_reconcile_status(
    message: Message,
    database: Database,
    settings: Settings,
    mediator_client: MediatorClient,
) -> None:
    if not is_admin(message, settings):
        return
    parts = (message.text or "").split(maxsplit=1)
    if len(parts) != 2:
        await message.answer("Формат: <code>/reconcile_status GUID</code>")
        return
    public_guid = parts[1].strip()
    async with database.session() as session:
        subscription = await SubscriptionRepository(session).get_by_public_guid(public_guid)
        if subscription is None:
            await message.answer("Подписка не найдена.")
            return
        local = await EntitlementRepository(session).get_for_subscription(subscription.id)
        operations = await EntitlementOperationRepository(session).list_for_subscription(
            subscription.id, limit=5
        )
    try:
        remote = await mediator_client.get_entitlement(public_guid)
    except MediatorClientError:
        logger.exception("Mediator entitlement status lookup failed.")
        await message.answer("Не удалось получить состояние Mediator.")
        return
    try:
        provenance = await mediator_client.get_entitlement_operation_by_result_version(
            public_guid,
            remote.version,
        )
    except MediatorClientError:
        logger.warning(
            "Mediator entitlement provenance lookup failed.",
            exc_info=True,
        )
        provenance = None
    local_text = (
        "отсутствует"
        if local is None
        else (
            f"<code>{escape_html(local.status)}</code>, version <b>{local.version}</b>, "
            f"до <code>{escape_html(to_aware_utc(local.valid_until_utc).isoformat())}</code>, "
            f"устройств <b>{local.max_device_tokens}</b>"
        )
    )
    operations_text = (
        "\n".join(
            f"• <code>{escape_html(item.operation_type)}</code>: "
            f"<code>{escape_html(item.state)}</code>"
            for item in operations
        )
        or "нет"
    )
    provenance_text = (
        "не найдена"
        if provenance is None
        else (
            f"<code>{escape_html(provenance.operation_type)}</code>, "
            f"operation <code>{escape_html(provenance.operation_id)}</code>"
        )
    )
    await message.answer(
        "<b>Состояние reconciliation</b>\n\n"
        f"GUID: <code>{escape_html(public_guid)}</code>\n"
        f"Business: <code>{escape_html(subscription.status)}</code>\n"
        f"Reconciliation: <code>{escape_html(subscription.reconciliation_state)}</code>\n"
        f"Причина: <code>{escape_html(subscription.reconciliation_reason or '—')}</code>\n\n"
        f"Local entitlement: {local_text}\n"
        f"Remote entitlement: <code>{escape_html(remote.status)}</code>, "
        f"version <b>{remote.version}</b>, "
        f"до <code>{escape_html(remote.valid_until_utc or '—')}</code>, "
        f"устройств <b>{remote.max_device_tokens}</b>\n"
        f"Remote provenance: {provenance_text}\n\n"
        f"Последние operations:\n{operations_text}"
    )


@router.message(Command("reconcile_restore_local"))
async def handle_reconcile_restore_local(
    message: Message,
    database: Database,
    settings: Settings,
    mediator_client: MediatorClient,
) -> None:
    await _handle_reconciliation_repair(
        message,
        database,
        settings,
        mediator_client,
        mode="restore_local",
    )


@router.message(Command("sync_expired"))
async def handle_sync_expired(
    message: Message,
    database: Database,
    settings: Settings,
    mediator_client: MediatorClient,
) -> None:
    if not is_admin(message, settings):
        return

    async with database.session() as session:
        service = ExpirationService(session, mediator_client)
        expired_count = await service.expire_due_subscriptions()

    await message.answer(
        f"Новых переходов active → expired: <b>{expired_count}</b>.\n\n"
        "Исторические расхождения и подписки в quarantine этой командой не восстанавливаются."
    )


@router.message(Command("test_user_reset"))
async def handle_test_user_reset(
    message: Message,
    database: Database,
    settings: Settings,
    mediator_client: MediatorClient,
) -> None:
    if not is_admin(message, settings) or message.from_user is None:
        return

    parts = (message.text or "").split()
    if len(parts) != 3 or not parts[1].isdigit():
        await message.answer(
            "Формат: <code>/test_user_reset TELEGRAM_ID CONFIRM_RESET_TELEGRAM_ID</code>"
        )
        return

    target_telegram_id = int(parts[1])
    expected_confirmation = f"CONFIRM_RESET_{target_telegram_id}"
    if parts[2] != expected_confirmation:
        await message.answer(
            f"Подтверждение не совпало. Используйте: <code>{expected_confirmation}</code>"
        )
        return

    if not settings.allow_test_user_reset:
        await message.answer(
            "Test user reset выключен. Установите "
            "<code>ALLOW_TEST_USER_RESET=true</code> только на время проверки."
        )
        return

    if target_telegram_id not in settings.test_user_reset_telegram_ids:
        await message.answer("Этот Telegram ID не входит в allowlist test reset.")
        return

    source_request_id = f"telegram:{message.chat.id}:{message.message_id}"
    try:
        async with database.session() as session:
            plan = await TestUserResetService(session).prepare(
                target_telegram_id,
                actor_telegram_id=message.from_user.id,
                source_request_id=source_request_id,
            )

        if plan.completed_outcome is not None:
            outcome = plan.completed_outcome
        else:
            for public_guid in plan.subscription_public_guids_to_disable:
                async with database.session() as session:
                    await AdminEntitlementAdjustmentService(session, mediator_client).apply(
                        public_guid=public_guid,
                        actor_telegram_id=message.from_user.id,
                        source_request_id=(f"test-user-reset:{source_request_id}:{public_guid}"),
                        reason="test_user_reset",
                        disable=True,
                    )

            async with database.session() as session:
                outcome = await TestUserResetService(session).finalize(
                    target_telegram_id,
                    actor_telegram_id=message.from_user.id,
                    source_request_id=source_request_id,
                )
    except ValueError as exception:
        safe_errors = {
            "test_user_not_found": "Пользователь не найден.",
            "test_user_reset_has_inflight_order": (
                "Есть оплачиваемый, активируемый или возвращаемый заказ. "
                "Сначала завершите его вручную."
            ),
            "test_user_reset_has_active_entitlement_operation": (
                "Есть незавершённая entitlement operation. Сначала выполните reconciliation."
            ),
            "test_user_reset_has_active_refund_operation": (
                "Есть незавершённая операция возврата."
            ),
            "test_user_reset_has_active_access_lease": (
                "Есть активная операция доступа. Подождите её завершения "
                "или выполните reconciliation."
            ),
            "test_user_reset_has_applying_referral_reward": (
                "Реферальная награда сейчас применяется. Дождитесь завершения операции."
            ),
            "test_user_reset_has_unreconciled_payment": (
                "Есть непримирённое подтверждение платежа. Reset остановлен."
            ),
            "test_user_reset_subscription_not_disabled": (
                "Не все подписки подтверждённо отключены в Mediator. Reset не завершён."
            ),
            "test_user_reset_operation_conflict": (
                "Идентификатор reset-операции уже использован для другого пользователя."
            ),
        }
        code = exception.args[0] if len(exception.args) == 1 else None
        await message.answer(safe_errors.get(code, "Test reset отклонён проверками безопасности."))
        return
    except Exception:
        logger.exception("Test user reset failed. Telegram id: %s", target_telegram_id)
        await message.answer(_admin_failure_text("test user reset"))
        return

    await message.answer(
        "✅ <b>Тестовый пользователь сброшен</b>\n\n"
        f"Telegram ID: <code>{outcome.telegram_id}</code>\n"
        f"Архивировано подписок: <b>{outcome.archived_subscriptions}</b>\n"
        f"Отменено ожидающих заказов: <b>{outcome.cancelled_orders}</b>\n"
        f"Закрыто расчётов: <b>{outcome.consumed_quotes}</b>\n"
        f"Отозвано скидок: <b>{outcome.revoked_discounts}</b>\n"
        f"Отменено ожидающих реферальных наград: "
        f"<b>{outcome.cancelled_referral_rewards}</b>\n\n"
        "Платёжная история и audit records сохранены. Старые платежи больше не "
        "блокируют trial для этого allowlist-пользователя. Можно заново проверить "
        "бесплатный период или назначить скидку 100% и пройти покупку."
    )
