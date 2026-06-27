from __future__ import annotations

from datetime import UTC, datetime

from aiogram import F, Router
from aiogram.enums import ChatType
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    LinkPreviewOptions,
)

from vpn_access_bot.advertising_readiness import CommerceOperationKind
from vpn_access_bot.client_catalog import ClientAppCatalog, Platform
from vpn_access_bot.config import Settings
from vpn_access_bot.credential_delivery import build_delivery_plan
from vpn_access_bot.db import Database
from vpn_access_bot.formatting import escape_html, format_local_date_ru
from vpn_access_bot.keyboards import (
    after_purchase_keyboard,
    back_to_main_keyboard,
    credential_delivery_keyboard,
    credential_failure_keyboard,
    first_fetch_check_keyboard,
    onboarding_access_keyboard,
    onboarding_install_keyboard,
    other_platforms_keyboard,
    platform_selection_keyboard,
    service_unavailable_keyboard,
    tv_platform_keyboard,
)
from vpn_access_bot.mediator_client import MediatorClient, MediatorClientError
from vpn_access_bot.models import utc_now
from vpn_access_bot.readiness import CommerceReadinessService
from vpn_access_bot.repositories import (
    NotificationDeliveryRepository,
    OnboardingSessionRepository,
    ProductEventRepository,
    SubscriptionRepository,
    UserRepository,
)
from vpn_access_bot.services import TrialService
from vpn_access_bot.trial import TrialEligibilityReason

router = Router(name="onboarding")


@router.callback_query(F.data == "trial:show")
async def handle_trial_show(
    callback: CallbackQuery,
    database: Database,
    settings: Settings,
    mediator_client: MediatorClient,
    readiness_service: CommerceReadinessService,
) -> None:
    await callback.answer()

    if callback.from_user is None or callback.message is None:
        return

    async with database.session() as session:
        service = TrialService(session, settings, mediator_client)
        eligibility = await service.get_eligibility(
            telegram_id=callback.from_user.id,
            username=callback.from_user.username,
            first_name=callback.from_user.first_name,
        )
        user = await UserRepository(session).get_by_telegram_id(callback.from_user.id)
        if user is not None:
            await ProductEventRepository(session).record(
                event_name="trial_offer_viewed",
                user_id=user.id,
            )
            await ProductEventRepository(session).record(
                event_name="trial_cta_opened",
                user_id=user.id,
                idempotency_key=f"trial_cta_opened:{callback.id}",
            )

    if not eligibility.is_available:
        text = _trial_unavailable_text(eligibility.reason)
        await callback.message.edit_text(text, reply_markup=back_to_main_keyboard())
        return

    readiness = await readiness_service.check(
        operation_kind=CommerceOperationKind.TRIAL,
        force=True,
    )
    if not readiness.can_sell:
        await callback.message.edit_text(
            "Сейчас нельзя выдать новое подключение.\n\n"
            "Оплата и бесплатная активация временно отключены, чтобы вы не получили "
            "неработающий доступ.",
            reply_markup=service_unavailable_keyboard("trial:show"),
        )
        return

    await callback.message.edit_text(
        "<b>Попробуйте VPN бесплатно 2 дня</b>\n\n"
        "• Оплата и банковская карта не нужны\n"
        "• Можно подключить 1 устройство\n"
        "• Бесплатный период доступен один раз\n"
        "• Срок начнётся после активации",
        reply_markup=_trial_confirm_keyboard(),
    )


@router.callback_query(F.data == "trial:activate")
async def handle_trial_activate(
    callback: CallbackQuery,
    database: Database,
    settings: Settings,
    mediator_client: MediatorClient,
    readiness_service: CommerceReadinessService,
) -> None:
    await callback.answer()

    if callback.from_user is None or callback.message is None:
        return

    readiness = await readiness_service.check(
        operation_kind=CommerceOperationKind.TRIAL,
        force=True,
    )
    if not readiness.can_sell:
        await callback.message.edit_text(
            "Сейчас нельзя выдать новое подключение.\n\n"
            "Попробуйте ещё раз немного позже — бесплатный период не считается использованным.",
            reply_markup=service_unavailable_keyboard("trial:activate"),
        )
        return

    async with database.session() as session:
        outcome = await TrialService(session, settings, mediator_client).activate_trial(
            telegram_id=callback.from_user.id,
            username=callback.from_user.username,
            first_name=callback.from_user.first_name,
        )

    if outcome.activated and outcome.claim is not None and outcome.claim.ends_at_utc is not None:
        trial_end = format_local_date_ru(outcome.claim.ends_at_utc, settings.subscription_time_zone)
        await callback.message.edit_text(
            f"<b>{escape_html(settings.product_name)}: бесплатный доступ активирован</b>\n\n"
            f"Действует до: <b>{trial_end}</b>\n"
            "Подключено устройств: <b>0 из 1</b>\n\n"
            "Осталось подключить это устройство.",
            reply_markup=after_purchase_keyboard(),
        )
        return

    if outcome.eligibility_reason in {
        TrialEligibilityReason.ALREADY_USED,
        TrialEligibilityReason.PAID_HISTORY_EXISTS,
    }:
        await callback.message.edit_text(
            "Бесплатный период уже недоступен.\n\n"
            "Можно купить доступ или открыть текущее состояние VPN в главном меню.",
            reply_markup=back_to_main_keyboard(),
        )
        return

    if outcome.eligibility_reason == TrialEligibilityReason.ACTIVE_SUBSCRIPTION_EXISTS:
        await callback.message.edit_text(
            "У вас уже есть действующий доступ. Бесплатный период не требуется.",
            reply_markup=after_purchase_keyboard(),
        )
        return

    if outcome.eligibility_reason == TrialEligibilityReason.ACTIVATION_IN_PROGRESS:
        await callback.message.edit_text(
            "Активация уже выполняется. Нажмите ещё раз через несколько секунд.",
            reply_markup=_trial_retry_keyboard(),
        )
        return

    await callback.message.edit_text(
        "Не получилось включить бесплатный доступ.\n\n"
        "Бесплатный период не обнулён. Можно повторить активацию с теми же условиями.",
        reply_markup=_trial_retry_keyboard(),
    )


@router.callback_query(F.data == "onboarding:install")
async def handle_platform_select(
    callback: CallbackQuery,
    database: Database,
) -> None:
    await callback.answer()

    if callback.from_user is None or callback.message is None:
        return

    preferred_platform = None

    async with database.session() as session:
        user = await UserRepository(session).get_by_telegram_id(callback.from_user.id)
        if user is not None:
            preferred_platform = user.platform_preference

    await callback.message.edit_text(
        "<b>Где установить Happ?</b>\n\nВыберите платформу только для инструкции по установке.",
        reply_markup=platform_selection_keyboard(preferred_platform),
    )


@router.callback_query(F.data == "onboarding:other")
async def handle_other_platforms(callback: CallbackQuery) -> None:
    await callback.answer()
    if callback.message is None:
        return
    await callback.message.edit_text(
        "<b>Другие устройства</b>\n\n"
        "Для Linux доступна обычная установка Happ. Для роутера может понадобиться помощь.",
        reply_markup=other_platforms_keyboard(),
    )


@router.callback_query(F.data == "onboarding:tv")
async def handle_tv_select(callback: CallbackQuery) -> None:
    await callback.answer()

    if callback.message is None:
        return

    await callback.message.edit_text(
        "<b>Какой у вас телевизор?</b>",
        reply_markup=tv_platform_keyboard(),
    )


@router.callback_query(F.data == "onboarding:router")
async def handle_router(callback: CallbackQuery) -> None:
    await callback.answer()

    if callback.message is None:
        return

    await callback.message.edit_text(
        "<b>Для роутера может понадобиться отдельная настройка</b>\n\n"
        "Напишите модель устройства в поддержку — так мы не предложим неподходящую инструкцию.",
        reply_markup=back_to_main_keyboard(),
    )


@router.callback_query(F.data.startswith("onboarding:platform:"))
async def handle_platform_chosen(
    callback: CallbackQuery,
    database: Database,
) -> None:
    await callback.answer()

    if callback.from_user is None or callback.message is None or callback.data is None:
        return

    try:
        platform = Platform(callback.data.rsplit(":", maxsplit=1)[-1])
        catalog_entry = ClientAppCatalog.default().get(platform)
    except ValueError:
        await callback.message.edit_text(
            "Не удалось определить тип устройства. Выберите его ещё раз.",
            reply_markup=platform_selection_keyboard(),
        )
        return

    async with database.session() as session:
        user = await UserRepository(session).get_or_create_from_message_user(
            telegram_id=callback.from_user.id,
            username=callback.from_user.username,
            first_name=callback.from_user.first_name,
        )
        subscription = await SubscriptionRepository(session).get_primary_for_user(user)
        onboarding_session = await OnboardingSessionRepository(session).start_or_update(
            user=user,
            subscription=subscription,
            platform=platform.value,
            current_step="app_installation",
            status="app_installation",
        )
        await ProductEventRepository(session).record(
            event_name="platform_selected",
            user_id=user.id,
            idempotency_key=f"platform_selected:{onboarding_session.id}:{platform.value}",
            payload={"platform": platform.value},
        )
        await ProductEventRepository(session).record(
            event_name="connect_device_started",
            user_id=user.id,
            idempotency_key=f"connect_device_started:{onboarding_session.id}",
            payload={"platform": platform.value},
        )
        await ProductEventRepository(session).record(
            event_name="client_install_opened",
            user_id=user.id,
            idempotency_key=f"client_install_opened:{onboarding_session.id}:{platform.value}",
            payload={"platform": platform.value},
        )

    await callback.message.edit_text(
        f"<b>Установка Happ: {catalog_entry.title_ru}</b>\n\n"
        "Установите приложение, затем нажмите «Открыть в Happ». "
        "Для всех устройств используется одна и та же ссылка подписки.",
        reply_markup=onboarding_install_keyboard(catalog_entry),
    )


@router.callback_query(F.data.startswith("onboarding:alt:"))
async def handle_alternate_install(callback: CallbackQuery) -> None:
    await callback.answer()

    if callback.message is None or callback.data is None:
        return

    try:
        platform = Platform(callback.data.rsplit(":", maxsplit=1)[-1])
        catalog_entry = ClientAppCatalog.default().get(platform)
    except ValueError:
        await callback.message.edit_text(
            "Не удалось определить тип устройства.",
            reply_markup=back_to_main_keyboard(),
        )
        return

    rows = [
        [InlineKeyboardButton(text=link.label, url=link.url)]
        for link in catalog_entry.install_links
    ]
    rows.extend(
        [
            [InlineKeyboardButton(text="Открыть в Happ", callback_data="credential:create")],
            [
                InlineKeyboardButton(
                    text="← Назад",
                    callback_data=f"onboarding:platform:{platform.value}",
                )
            ],
        ]
    )
    await callback.message.edit_text(
        f"<b>Официальные варианты установки для {catalog_entry.title_ru}</b>\n\n"
        "Выберите подходящий источник.",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=rows),
    )


@router.callback_query(F.data == "onboarding:installed")
async def handle_app_installed(
    callback: CallbackQuery,
    database: Database,
    settings: Settings,
) -> None:
    await callback.answer()

    if callback.from_user is None or callback.message is None:
        return

    async with database.session() as session:
        user = await UserRepository(session).get_by_telegram_id(callback.from_user.id)
        if user is None:
            await callback.message.edit_text(
                "Сначала активируйте доступ к VPN.",
                reply_markup=back_to_main_keyboard(),
            )
            return
        subscription = await SubscriptionRepository(session).get_primary_for_user(user)
        if subscription is None:
            await callback.message.edit_text(
                "Сначала активируйте бесплатный период или купите доступ.",
                reply_markup=back_to_main_keyboard(),
            )
            return
        await OnboardingSessionRepository(session).start_or_update(
            user=user,
            subscription=subscription,
            platform=user.platform_preference,
            current_step="access_delivery",
            status="access_delivery",
        )

    await callback.message.edit_text(
        f"<b>Теперь добавим {escape_html(settings.product_name)} в Happ</b>\n\n"
        "Бот выдаст одну ссылку подписки. Её можно открыть в Happ на всех ваших устройствах.",
        reply_markup=onboarding_access_keyboard(settings.product_name),
    )


@router.callback_query(F.data == "onboarding:any")
@router.callback_query(F.data == "subscription:link")
@router.callback_query(F.data.in_({"credential:create", "handoff:create"}))
async def handle_credential_create(
    callback: CallbackQuery,
    database: Database,
    mediator_client: MediatorClient,
    readiness_service: CommerceReadinessService,
    settings: Settings,
) -> None:
    await callback.answer()

    if callback.from_user is None or callback.message is None:
        return
    if callback.message.chat.type != ChatType.PRIVATE:
        return

    async with database.session() as session:
        user = await UserRepository(session).get_by_telegram_id(callback.from_user.id)
        if user is None:
            await callback.message.edit_text(
                "Сначала активируйте доступ к VPN.",
                reply_markup=back_to_main_keyboard(),
            )
            return
        subscription = await SubscriptionRepository(session).get_primary_for_user(user)
        onboarding_repository = OnboardingSessionRepository(session)
        onboarding_session = await onboarding_repository.get_open_for_user(user.id)
        preferred_platform = (
            onboarding_session.platform
            if onboarding_session is not None and onboarding_session.platform is not None
            else user.platform_preference
        )
        user_id = user.id
        subscription_id = subscription.id if subscription is not None else None
        subscription_guid = subscription.public_guid if subscription is not None else None
        if subscription is not None:
            await onboarding_repository.start_or_update(
                user=user,
                subscription=subscription,
                platform=preferred_platform,
                current_step="access_delivery",
                status="access_delivery",
            )

    if subscription_guid is None or subscription_id is None:
        await callback.message.edit_text(
            "Сначала активируйте бесплатный период или купите доступ.",
            reply_markup=back_to_main_keyboard(),
        )
        return

    readiness = await readiness_service.check(
        operation_kind=CommerceOperationKind.ISSUE_EXISTING_FEED,
        force=True,
    )
    if not readiness.can_sell:
        message = (
            "Обновление сервиса устройств ещё не завершено.\n\n"
            "Ваш действующий VPN не отключён. Новую ссылку можно будет выдать после "
            "завершения совместимого обновления."
            if readiness.reason_code == "device_issuance_v2_unavailable"
            else "Сервис устройств временно недоступен.\n\n"
            "Ваш действующий VPN не отключён. Попробуйте открыть подписку позже."
        )
        await callback.message.edit_text(
            message,
            reply_markup=credential_failure_keyboard(),
        )
        return

    try:
        credential = await mediator_client.ensure_subscription_feed(subscription_guid)
        delivery = build_delivery_plan(
            credential.connection_url,
            happ_deep_link_template=settings.happ_deep_link_template,
            primary_subscription_base_url=settings.public_subscription_base_url,
            fallback_subscription_base_url=settings.fallback_subscription_base_url,
        )
    except MediatorClientError as exception:
        text = (
            "Слишком много попыток.\n\nПовторите подключение немного позже."
            if exception.error_code == "rate_limited"
            else "Не получилось подготовить ссылку подписки.\n\n"
            "Доступ не изменён. Попробуйте ещё раз."
        )
        await callback.message.edit_text(
            text,
            reply_markup=credential_failure_keyboard(),
        )
        return

    async with database.session() as session:
        user = await UserRepository(session).get_by_telegram_id(callback.from_user.id)
        if user is None:
            return
        subscription = await SubscriptionRepository(session).get_by_id(subscription_id)
        onboarding_session = await OnboardingSessionRepository(session).start_or_update(
            user=user,
            subscription=subscription,
            platform=preferred_platform,
            current_step="waiting_first_fetch",
            status="waiting_first_fetch",
        )
        onboarding_session.device_public_id = None
        onboarding_session.handoff_claim_id = None
        onboarding_session.updated_at_utc = utc_now()
        await ProductEventRepository(session).record(
            event_name="subscription_feed_issued",
            user_id=user_id,
            idempotency_key=f"subscription_feed_issued:{subscription_guid}",
            payload={
                "platform": preferred_platform or "unknown",
                "result": credential.status,
            },
        )

    await callback.message.edit_text(
        "<b>Ссылка подписки готова</b>\n\nИспользуйте эту же ссылку на всех своих устройствах.",
        reply_markup=back_to_main_keyboard(),
    )
    copy_note = (
        "Нажмите «Скопировать ссылку», затем откройте Happ: "
        "Подписки → Добавить → Вставить из буфера."
        if delivery.can_copy
        else "Нажмите и удерживайте текст, скопируйте его целиком, затем вставьте в Happ."
    )
    await callback.message.answer(
        "<b>Ссылка подписки</b>\n\n"
        f"<code>{escape_html(delivery.connection_url)}</code>\n\n"
        f"{copy_note}",
        reply_markup=credential_delivery_keyboard(
            delivery.connection_url,
            happ_deep_link=delivery.happ_deep_link,
            can_copy=delivery.can_copy,
        ),
        link_preview_options=LinkPreviewOptions(is_disabled=True),
    )


@router.callback_query(F.data == "credential:delete_message")
async def handle_delete_credential_message(callback: CallbackQuery) -> None:
    await callback.answer("Сообщение удалено")
    if callback.message is not None and callback.message.chat.type == ChatType.PRIVATE:
        await callback.message.delete()


@router.callback_query(F.data.in_({"credential:check", "handoff:check"}))
async def handle_first_fetch_check(
    callback: CallbackQuery,
    database: Database,
    mediator_client: MediatorClient,
    settings: Settings,
) -> None:
    await callback.answer()

    if callback.from_user is None or callback.message is None:
        return

    async with database.session() as session:
        user = await UserRepository(session).get_by_telegram_id(callback.from_user.id)
        onboarding_session = (
            await OnboardingSessionRepository(session).get_open_for_user(user.id)
            if user is not None
            else None
        )
        subscription = (
            await SubscriptionRepository(session).get_primary_for_user(user)
            if user is not None
            else None
        )
        user_id = user.id if user is not None else None
        onboarding_session_id = onboarding_session.id if onboarding_session is not None else None
        device_public_id = (
            onboarding_session.device_public_id or onboarding_session.handoff_claim_id
            if onboarding_session is not None
            else None
        )
        subscription_guid = subscription.public_guid if subscription is not None else None
        subscription_id = subscription.id if subscription is not None else None
        feed_issued_at_utc = (
            onboarding_session.updated_at_utc if onboarding_session is not None else None
        )

    if (
        user_id is None
        or onboarding_session_id is None
        or subscription_guid is None
        or subscription_id is None
    ):
        await callback.message.edit_text(
            "Сначала получите ссылку подписки.",
            reply_markup=onboarding_access_keyboard(settings.product_name),
        )
        return

    try:
        devices = await mediator_client.list_device_tokens(subscription_guid)
    except MediatorClientError:
        await callback.message.edit_text(
            "Не удалось проверить получение подписки.\n\n"
            "Ваш доступ не изменён. Попробуйте ещё раз через минуту.",
            reply_markup=first_fetch_check_keyboard(),
        )
        return

    target_device = (
        next((device for device in devices if device.public_id == device_public_id), None)
        if device_public_id is not None
        else next(
            (
                device
                for device in devices
                if device.access_channel == "unified_feed"
                and device.first_fetched_at_utc
                and (
                    feed_issued_at_utc is None
                    or _parse_mediator_datetime(device.last_used_at_utc) >= feed_issued_at_utc
                )
            ),
            None,
        )
    )

    if target_device is not None and target_device.first_fetched_at_utc:
        notification_delivery_id: int | None = None
        async with database.session() as session:
            await ProductEventRepository(session).record(
                event_name="credential_first_fetched",
                user_id=user_id,
                idempotency_key=f"credential_first_fetched:{target_device.public_id}",
            )
            await ProductEventRepository(session).record(
                event_name="subscription_observed_by_client",
                user_id=user_id,
                idempotency_key=f"subscription_observed_by_client:{target_device.public_id}",
            )
            completed = await OnboardingSessionRepository(session).mark_completed(
                onboarding_session_id,
                target_device.public_id,
            )
            if completed:
                await ProductEventRepository(session).record(
                    event_name="onboarding_completed",
                    user_id=user_id,
                    idempotency_key=f"onboarding_completed:{onboarding_session_id}",
                )
                delivery = await NotificationDeliveryRepository(session).claim(
                    subscription_id,
                    "onboarding_completed",
                    f"onboarding_completed:{onboarding_session_id}",
                )
                notification_delivery_id = delivery.id if delivery is not None else None

        await callback.message.edit_text(
            f"<b>Happ получил подписку {escape_html(settings.product_name)}</b>\n\n"
            "Список серверов загружен. Это подтверждает получение подписки, "
            "но не является проверкой поднятого VPN-туннеля.",
            reply_markup=back_to_main_keyboard(),
        )
        if notification_delivery_id is not None:
            async with database.session() as session:
                await NotificationDeliveryRepository(session).mark_delivered(
                    notification_delivery_id
                )
        return

    await callback.message.edit_text(
        "Happ пока не запрашивал подписку.\n\n"
        "Добавьте ссылку в Happ и обновите подписку. Если она всё ещё не появилась, "
        "нажмите кнопку ниже ещё раз.",
        reply_markup=first_fetch_check_keyboard(),
    )


def _parse_mediator_datetime(value: str | None) -> datetime:
    if not value:
        return datetime.min.replace(tzinfo=UTC)
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def _device_display_name(platform: str | None) -> str:
    label = {
        "android": "Android",
        "ios": "iPhone / iPad",
        "windows": "Windows",
        "macos": "macOS",
        "linux": "Linux",
        "android_tv": "Android TV",
        "apple_tv": "Apple TV",
    }.get(platform or "", "устройство")
    return f"Happ · {label}"


def _trial_unavailable_text(reason: TrialEligibilityReason) -> str:
    if reason == TrialEligibilityReason.PAID_HISTORY_EXISTS:
        return "Бесплатный период доступен только до первой успешной оплаты."
    if reason == TrialEligibilityReason.ACTIVE_SUBSCRIPTION_EXISTS:
        return "У вас уже есть действующий доступ к VPN."
    if reason == TrialEligibilityReason.ACTIVATION_IN_PROGRESS:
        return "Бесплатный доступ уже активируется. Попробуйте проверить состояние чуть позже."
    if reason == TrialEligibilityReason.FEATURE_DISABLED:
        return "Бесплатный период сейчас отключён."
    return "Бесплатный период уже был использован."


def _trial_confirm_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="Активировать бесплатные 2 дня",
                    callback_data="trial:activate",
                )
            ],
            [InlineKeyboardButton(text="🏠 Главное меню", callback_data="menu:main")],
        ]
    )


def _trial_retry_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="Повторить активацию", callback_data="trial:activate")],
            [InlineKeyboardButton(text="Помощь", callback_data="support:show")],
            [InlineKeyboardButton(text="🏠 Главное меню", callback_data="menu:main")],
        ]
    )
