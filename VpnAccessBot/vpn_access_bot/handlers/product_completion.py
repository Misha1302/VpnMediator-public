from __future__ import annotations

from datetime import timedelta
from html import escape

from aiogram import F, Router
from aiogram.filters import Command
from aiogram.types import CallbackQuery, Message
from sqlalchemy import func, select

from vpn_access_bot.advertising_readiness import CommerceOperationKind
from vpn_access_bot.config import Settings
from vpn_access_bot.constants import ORDER_STATUS_ACTIVATION_FAILED, REFERRAL_REWARD_PERCENT
from vpn_access_bot.db import Database
from vpn_access_bot.keyboards import (
    after_purchase_keyboard,
    back_to_main_keyboard,
    back_to_more_keyboard,
    service_unavailable_keyboard,
)
from vpn_access_bot.mediator_client import MediatorClient
from vpn_access_bot.models import (
    AcquisitionCampaign,
    ProductEvent,
    ReferralReward,
    User,
    UserAcquisition,
    UserDiscount,
    WorkerHealth,
    utc_now,
)
from vpn_access_bot.product_completion import (
    cancel_latest_pending_order,
    close_support_request,
)
from vpn_access_bot.product_events import FUNNEL_EVENT_ORDER, ProductEventName
from vpn_access_bot.readiness import CommerceReadinessService
from vpn_access_bot.repositories import OrderRepository, UserRepository
from vpn_access_bot.services import PurchaseService

router = Router(name="product_completion")


def _is_admin(message: Message, settings: Settings) -> bool:
    return message.from_user is not None and message.from_user.id in settings.admin_telegram_ids


def _parts(message: Message) -> list[str]:
    return (message.text or "").split()


def _referral_text(link: str, applied_count: int, applied_seconds: int) -> str:
    thirty_day_reward = 30 * REFERRAL_REWARD_PERCENT // 100
    six_month_reward = 180 * REFERRAL_REWARD_PERCENT // 100
    return (
        "<b>Пригласить друга</b>\n\n"
        f"Друг оплатит 30 дней — вам добавится {thirty_day_reward} дней.\n"
        f"Оплатит 6 месяцев — вам добавится примерно {six_month_reward} дней.\n\n"
        "Бонус начисляется только за реально оплаченное время. "
        "Бесплатные дни, тестовые покупки и увеличение только числа устройств "
        "бонус не дают.\n\n"
        f"Ваша ссылка:\n<code>{link}</code>\n\n"
        f"Начислений: <b>{applied_count}</b>\n"
        f"Начислено дней: <b>{applied_seconds // 86400}</b>"
    )


@router.message(Command("referral"))
async def handle_referral(message: Message, database: Database) -> None:
    if message.from_user is None:
        return

    async with database.session() as session:
        user = await UserRepository(session).get_or_create_from_message_user(
            telegram_id=message.from_user.id,
            username=message.from_user.username,
            first_name=message.from_user.first_name,
        )
        reward_result = await session.execute(
            select(
                func.count(ReferralReward.id),
                func.coalesce(func.sum(ReferralReward.reward_duration_seconds), 0),
            ).where(
                ReferralReward.referrer_user_id == user.id,
                ReferralReward.status == "applied",
            )
        )
        applied_count, applied_seconds = reward_result.one()

    bot_user = await message.bot.get_me()
    link = f"https://t.me/{bot_user.username}?start=ref_{user.referral_code}"
    await message.answer(
        _referral_text(link, int(applied_count), int(applied_seconds)),
        reply_markup=back_to_main_keyboard(),
    )


@router.callback_query(F.data == "referral:show")
async def handle_referral_callback(callback: CallbackQuery, database: Database) -> None:
    await callback.answer()

    if callback.from_user is None or callback.message is None:
        return

    async with database.session() as session:
        user = await UserRepository(session).get_or_create_from_message_user(
            telegram_id=callback.from_user.id,
            username=callback.from_user.username,
            first_name=callback.from_user.first_name,
        )
        reward_result = await session.execute(
            select(
                func.count(ReferralReward.id),
                func.coalesce(func.sum(ReferralReward.reward_duration_seconds), 0),
            ).where(
                ReferralReward.referrer_user_id == user.id,
                ReferralReward.status == "applied",
            )
        )
        applied_count, applied_seconds = reward_result.one()

    bot_user = await callback.bot.get_me()
    link = f"https://t.me/{bot_user.username}?start=ref_{user.referral_code}"
    await callback.message.edit_text(
        _referral_text(link, int(applied_count), int(applied_seconds)),
        reply_markup=back_to_more_keyboard(),
    )


@router.message(Command("referral_stats"))
async def handle_referral_stats(
    message: Message,
    database: Database,
    settings: Settings,
) -> None:
    if not _is_admin(message, settings):
        return

    async with database.session() as session:
        result = await session.execute(
            select(ReferralReward.status, func.count(ReferralReward.id)).group_by(
                ReferralReward.status
            )
        )
        rows = list(result.all())

    text = "\n".join(f"{escape(status)}: <b>{count}</b>" for status, count in rows)
    await message.answer("<b>Реферальные начисления</b>\n\n" + (text or "Начислений пока нет."))


@router.message(Command("discount_set"))
async def handle_discount_set(
    message: Message,
    database: Database,
    settings: Settings,
) -> None:
    if not _is_admin(message, settings) or message.from_user is None:
        return

    parts = _parts(message)

    if len(parts) < 3:
        await message.answer(
            "Использование: <code>/discount_set TELEGRAM_ID PERCENT "
            "[VALID_DAYS] [MAX_USES] [SCOPE] [REASON]</code>"
        )
        return

    try:
        telegram_id = int(parts[1])
        percent = int(parts[2])
        valid_days = int(parts[3]) if len(parts) > 3 else None
        max_uses = int(parts[4]) if len(parts) > 4 else None
        scope = parts[5] if len(parts) > 5 else "all"
        reason = " ".join(parts[6:]) if len(parts) > 6 else None
    except ValueError:
        await message.answer("Числовой аргумент указан неверно.")
        return

    allowed_scopes = {
        "all",
        "purchase",
        "extend",
        "upgrade_devices",
        "extend_and_upgrade",
        "resume",
    }

    if not 1 <= percent <= 100 or scope not in allowed_scopes:
        await message.answer(
            "Скидка должна быть от 1 до 100%, а область действия — поддерживаемой операцией."
        )
        return

    if valid_days is not None and valid_days < 1:
        await message.answer("VALID_DAYS должен быть положительным числом.")
        return

    if max_uses is not None and max_uses < 1:
        await message.answer("MAX_USES должен быть положительным числом.")
        return

    async with database.session() as session:
        user = await UserRepository(session).get_by_telegram_id(telegram_id)

        if user is None:
            await message.answer("Пользователь не найден.")
            return

        current_result = await session.execute(
            select(UserDiscount).where(
                UserDiscount.user_id == user.id,
                UserDiscount.status == "active",
            )
        )

        now = utc_now()

        for current in current_result.scalars().all():
            current.status = "revoked"
            current.revoked_at_utc = now
            current.revoked_by_admin_telegram_id = message.from_user.id

        discount = UserDiscount(
            user_id=user.id,
            discount_bps=percent * 100,
            scope=scope,
            starts_at_utc=now,
            expires_at_utc=now + timedelta(days=valid_days) if valid_days else None,
            max_uses=max_uses,
            used_count=0,
            status="active",
            reason=reason,
            created_by_admin_telegram_id=message.from_user.id,
            created_at_utc=now,
        )
        session.add(discount)

    await message.answer(
        f"Personal discount set: <b>{percent}%</b>, user <code>{telegram_id}</code>, "
        f"scope <code>{escape(scope)}</code>."
    )

    try:
        await message.bot.send_message(
            telegram_id,
            f"Для вас назначена персональная скидка <b>{percent}%</b>. "
            "Она применится автоматически при создании нового заказа.",
        )
    except Exception:
        await message.answer("Скидка сохранена, но уведомить пользователя не удалось.")


@router.message(Command("discount_show"))
async def handle_discount_show(
    message: Message,
    database: Database,
    settings: Settings,
) -> None:
    if not _is_admin(message, settings):
        return

    parts = _parts(message)

    if len(parts) != 2 or not parts[1].isdigit():
        await message.answer("Использование: <code>/discount_show TELEGRAM_ID</code>")
        return

    telegram_id = int(parts[1])

    async with database.session() as session:
        user = await UserRepository(session).get_by_telegram_id(telegram_id)

        if user is None:
            await message.answer("Пользователь не найден.")
            return

        result = await session.execute(
            select(UserDiscount)
            .where(UserDiscount.user_id == user.id, UserDiscount.status == "active")
            .order_by(UserDiscount.created_at_utc.desc())
        )
        discount = result.scalars().first()

    if discount is None:
        await message.answer("Активная скидка не найдена.")
        return

    await message.answer(
        f"Discount: <b>{discount.discount_bps / 100:g}%</b>\n"
        f"Scope: <code>{escape(discount.scope)}</code>\n"
        f"Uses: <b>{discount.used_count}</b>"
        + (f" / <b>{discount.max_uses}</b>" if discount.max_uses is not None else "")
    )


@router.message(Command("discount_list"))
async def handle_discount_list(
    message: Message,
    database: Database,
    settings: Settings,
) -> None:
    if not _is_admin(message, settings):
        return

    async with database.session() as session:
        result = await session.execute(
            select(UserDiscount, User)
            .join(User, User.id == UserDiscount.user_id)
            .where(UserDiscount.status == "active")
            .order_by(UserDiscount.created_at_utc.desc())
            .limit(100)
        )
        rows = list(result.all())

    if not rows:
        await message.answer("Активных скидок нет.")
        return

    lines = [
        (
            f"<code>{user.telegram_id}</code>: "
            f"<b>{discount.discount_bps / 100:g}%</b>, "
            f"scope=<code>{escape(discount.scope)}</code>, "
            f"uses={discount.used_count}"
        )
        for discount, user in rows
    ]
    await message.answer("<b>Active discounts</b>\n\n" + "\n".join(lines))


@router.message(Command("discount_remove"))
async def handle_discount_remove(
    message: Message,
    database: Database,
    settings: Settings,
) -> None:
    if not _is_admin(message, settings) or message.from_user is None:
        return

    parts = _parts(message)

    if len(parts) != 2 or not parts[1].isdigit():
        await message.answer("Использование: <code>/discount_remove TELEGRAM_ID</code>")
        return

    telegram_id = int(parts[1])

    async with database.session() as session:
        user = await UserRepository(session).get_by_telegram_id(telegram_id)

        if user is None:
            await message.answer("Пользователь не найден.")
            return

        result = await session.execute(
            select(UserDiscount).where(
                UserDiscount.user_id == user.id,
                UserDiscount.status == "active",
            )
        )
        discounts = list(result.scalars().all())
        now = utc_now()

        for discount in discounts:
            discount.status = "revoked"
            discount.revoked_at_utc = now
            discount.revoked_by_admin_telegram_id = message.from_user.id

    await message.answer(f"Отозвано активных скидок: <b>{len(discounts)}</b>.")


@router.message(Command("support_close"))
async def handle_support_close(
    message: Message,
    database: Database,
    settings: Settings,
) -> None:
    if not _is_admin(message, settings) or message.from_user is None:
        return

    parts = _parts(message)

    if len(parts) != 2:
        await message.answer("Использование: <code>/support_close REQUEST_ID_PREFIX</code>")
        return

    async with database.session() as session:
        request = await close_support_request(
            session,
            parts[1],
            message.from_user.id,
        )

        if request is None:
            await message.answer("Открытое обращение не найдено или префикс неоднозначен.")
            return

        user = await UserRepository(session).get_by_id(request.user_id)

    await message.answer(f"Обращение <code>{request.public_id[:8]}</code> закрыто.")

    if user is not None:
        try:
            await message.bot.send_message(
                user.telegram_id,
                f"Обращение №{request.public_id[:8]} закрыто. "
                "При необходимости создайте новое обращение через меню поддержки.",
            )
        except Exception:
            await message.answer("Обращение закрыто, но уведомить пользователя не удалось.")


@router.message(Command("workers"))
async def handle_workers(
    message: Message,
    database: Database,
    settings: Settings,
) -> None:
    if not _is_admin(message, settings):
        return

    async with database.session() as session:
        result = await session.execute(select(WorkerHealth).order_by(WorkerHealth.worker_name))
        workers = list(result.scalars().all())

    if not workers:
        await message.answer("Данные о фоновых задачах ещё не появились.")
        return

    lines = [
        (
            f"<code>{escape(worker.worker_name)}</code>: "
            f"успех={escape(str(worker.last_success_at_utc or 'никогда'))}, "
            f"ошибка={escape(str(worker.last_failure_at_utc or 'никогда'))}, "
            f"код={escape(worker.last_error_code or 'нет')}"
        )
        for worker in workers
    ]
    await message.answer("<b>Состояние фоновых задач</b>\n\n" + "\n".join(lines))


@router.callback_query(F.data.startswith("order:cancel:"))
async def handle_cancel_order(callback: CallbackQuery, database: Database) -> None:
    await callback.answer()

    if callback.from_user is None or callback.message is None or callback.data is None:
        return

    public_order_id = callback.data.split(":", maxsplit=2)[2]
    async with database.session() as session:
        user = await UserRepository(session).get_by_telegram_id(callback.from_user.id)
        order = (
            await cancel_latest_pending_order(session, user.id, public_order_id)
            if user is not None
            else None
        )

    await callback.message.edit_text(
        "Незавершённый заказ отменён."
        if order is not None
        else "Заказ не найден, уже оплачен или больше не может быть отменён.",
        reply_markup=back_to_main_keyboard(),
    )


@router.callback_query(F.data.startswith("order:retry:"))
async def handle_retry_order(
    callback: CallbackQuery,
    database: Database,
    settings: Settings,
    mediator_client: MediatorClient,
    readiness_service: CommerceReadinessService,
) -> None:
    await callback.answer()

    if callback.from_user is None or callback.message is None or callback.data is None:
        return

    readiness = await readiness_service.check(
        operation_kind=CommerceOperationKind.RETRY_ACTIVATION,
        force=True,
    )
    if not readiness.can_sell:
        await callback.message.edit_text(
            "Новый срок пока нельзя применить, но полученная оплата сохранена.\n\n"
            "Текущий VPN продолжает работать до прежней даты.",
            reply_markup=service_unavailable_keyboard(callback.data),
        )
        return

    public_order_id = callback.data.split(":", maxsplit=2)[2]
    async with database.session() as session:
        user = await UserRepository(session).get_by_telegram_id(callback.from_user.id)
        order = (
            await OrderRepository(session).get_by_public_id_for_user(public_order_id, user.id)
            if user is not None
            else None
        )

    if order is None or order.status != ORDER_STATUS_ACTIVATION_FAILED:
        await callback.message.edit_text(
            "Заказ для повторной активации не найден.",
            reply_markup=back_to_main_keyboard(),
        )
        return

    async with database.session() as session:
        outcome = await PurchaseService(
            session,
            settings,
            mediator_client,
        ).retry_activation_by_id(order.id)

    if outcome.subscription is None:
        await callback.message.edit_text(
            "Доступ пока не удалось применить. Оплата сохранена; "
            "попробуйте позже или откройте поддержку.",
            reply_markup=back_to_main_keyboard(),
        )
        return

    await callback.message.edit_text(
        "Доступ восстановлен. Можно подключить устройство.",
        reply_markup=after_purchase_keyboard(),
    )


@router.message(Command("product_funnel"))
async def handle_product_funnel(
    message: Message,
    database: Database,
    settings: Settings,
) -> None:
    if not _is_admin(message, settings):
        return

    parts = _parts(message)
    try:
        days = int(parts[1]) if len(parts) > 1 else 7
    except ValueError:
        await message.answer("Формат: <code>/product_funnel [DAYS]</code>")
        return
    if not 1 <= days <= 90:
        await message.answer("Период должен быть от 1 до 90 дней.")
        return

    cohort_start = utc_now() - timedelta(days=days)
    conversion_horizon = timedelta(days=7)
    async with database.session() as session:
        anchor_result = await session.execute(
            select(
                ProductEvent.user_id,
                func.min(ProductEvent.occurred_at_utc),
                AcquisitionCampaign.channel,
                AcquisitionCampaign.placement,
            )
            .outerjoin(
                UserAcquisition,
                UserAcquisition.user_id == ProductEvent.user_id,
            )
            .outerjoin(
                AcquisitionCampaign,
                AcquisitionCampaign.id == UserAcquisition.first_campaign_id,
            )
            .where(
                ProductEvent.event_name == ProductEventName.FIRST_START,
                ProductEvent.occurred_at_utc >= cohort_start,
                ProductEvent.user_id.is_not(None),
            )
            .group_by(
                ProductEvent.user_id,
                AcquisitionCampaign.channel,
                AcquisitionCampaign.placement,
            )
        )
        anchors = anchor_result.all()
        user_ids = [int(row[0]) for row in anchors]
        events_by_user: dict[int, list[tuple[str, object]]] = {}
        if user_ids:
            event_result = await session.execute(
                select(
                    ProductEvent.user_id,
                    ProductEvent.event_name,
                    ProductEvent.occurred_at_utc,
                ).where(
                    ProductEvent.user_id.in_(user_ids),
                    ProductEvent.event_name.in_([str(item) for item in FUNNEL_EVENT_ORDER]),
                    ProductEvent.occurred_at_utc >= cohort_start,
                )
            )
            for user_id, event_name, occurred_at in event_result.all():
                events_by_user.setdefault(int(user_id), []).append((str(event_name), occurred_at))

    labels = {
        ProductEventName.FIRST_START: "Запустили бота",
        ProductEventName.TRIAL_ACTIVATED: "Активировали бесплатный период",
        ProductEventName.PAYMENT_COMPLETED: "Завершили оплату",
        ProductEventName.ACTIVATION_COMPLETED: "Получили платный доступ",
        ProductEventName.SUBSCRIPTION_OBSERVED_BY_CLIENT: "Happ загрузил подписку",
        ProductEventName.ONBOARDING_COMPLETED: "Завершили подключение",
        ProductEventName.REFUND_COMPLETED: "Оформили возврат",
    }
    cohorts: dict[str, dict[ProductEventName, int]] = {}
    for user_id, first_start_at, channel, placement in anchors:
        cohort_key = channel or "organic_unknown"
        if placement:
            cohort_key += f" / {placement}"
        counts = cohorts.setdefault(
            cohort_key,
            {event_name: 0 for event_name in FUNNEL_EVENT_ORDER},
        )
        horizon_end = first_start_at + conversion_horizon
        user_events = events_by_user.get(int(user_id), [])
        for event_name in FUNNEL_EVENT_ORDER:
            if any(
                recorded_name == str(event_name) and first_start_at <= occurred_at <= horizon_end
                for recorded_name, occurred_at in user_events
            ):
                counts[event_name] += 1

    lines = [
        f"<b>Когортная продуктовая воронка за {days} дней</b>",
        "Горизонт конверсии: 7 дней; каждый этап считается для тех же пользователей.",
    ]
    if not cohorts:
        lines.extend(["", "Нет новых когорт с событием first_start."])
    for cohort_name, counts in sorted(cohorts.items()):
        lines.extend(["", f"<b>{escape(cohort_name)}</b>"])
        for event_name in FUNNEL_EVENT_ORDER:
            lines.append(f"{labels[event_name]}: <b>{counts[event_name]}</b>")
        started = counts[ProductEventName.FIRST_START]
        completed = counts[ProductEventName.ONBOARDING_COMPLETED]
        if started:
            lines.append(f"/start → подключение: <b>{completed * 100 / started:.1f}%</b>")

    await message.answer("\n".join(lines))
