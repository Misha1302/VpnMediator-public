from __future__ import annotations

from vpn_access_bot.commerce import (
    CabinetState,
    OrderNoticeState,
    SubscriptionLifecycleState,
)
from vpn_access_bot.formatting import escape_html, format_access_through_date_ru


def cabinet_text(
    state: CabinetState,
    product_name: str,
    business_timezone: str = "Europe/Moscow",
) -> str:
    header = f"<b>{escape_html(product_name)}</b>"
    if state.order_notice_state != OrderNoticeState.NONE:
        return _pending_order_text(state, business_timezone)

    if state.subscription_state == SubscriptionLifecycleState.NONE:
        lines = [
            header,
            "",
            "Простой VPN для телефона и компьютера.",
        ]
        if state.trial_available:
            lines.extend(
                [
                    "",
                    "Попробуйте 2 дня бесплатно — без карты.",
                    "Подключение занимает несколько минут, а при проблеме можно открыть помощь.",
                ]
            )
        elif not state.commerce_available:
            lines.extend(
                [
                    "",
                    "Сейчас нельзя выдать новое подключение. Оплата временно отключена, "
                    "чтобы с вас не списались деньги без готового доступа.",
                ]
            )
        return "\n".join(lines)

    if state.valid_until_utc is None:
        return f"{header}\n\nСтатус нужно уточнить через помощь."

    access_through = format_access_through_date_ru(state.valid_until_utc, business_timezone)

    if state.subscription_state == SubscriptionLifecycleState.EXPIRED:
        return (
            f"{header}\n\n"
            f"<b>🔴 Доступ закончился</b>\n\nДоступ действовал до: <b>{access_through}</b>"
        )

    if state.subscription_state == SubscriptionLifecycleState.DISABLED:
        return (
            f"{header}\n\n"
            "<b>⚠️ Доступ временно отключён</b>\n\n"
            f"Срок доступа: <b>{access_through}</b>\n"
            "Откройте помощь: самостоятельное возобновление недоступно."
        )

    device_line = (
        f"Устройства: <b>{state.active_device_tokens} из {state.max_device_tokens}</b>"
        if state.mediator_available and state.active_device_tokens is not None
        else "Устройства: <b>временно не удалось проверить</b>"
    )
    return f"{header}\n\n🟢 Доступ активен\nДействует до: <b>{access_through}</b>\n{device_line}"


def commerce_unavailable_text() -> str:
    return (
        "<b>Новые подключения временно недоступны</b>\n\n"
        "Сейчас нельзя выдать новое подключение. Мы уже проверяем сервис. "
        "Оплата временно отключена, чтобы с вас не списались деньги без готового доступа."
    )


def _pending_order_text(state: CabinetState, business_timezone: str) -> str:
    if state.order_notice_state == OrderNoticeState.PAYMENT_PENDING:
        return "<b>Оплата ещё не завершена</b>\n\nЗаказ сохранён. Новую покупку создавать не нужно."

    if state.order_notice_state in {
        OrderNoticeState.PAYMENT_RECEIVED,
        OrderNoticeState.ACTIVATING,
    }:
        return (
            "<b>Оплата получена</b>\n\n"
            "Мы заканчиваем включение доступа. Повторно оплачивать не нужно."
        )

    lines = [
        "<b>⚠️ Не удалось включить доступ автоматически</b>",
        "",
        "Оплата сохранена. Повторно платить не нужно.",
    ]
    if (
        state.subscription_state == SubscriptionLifecycleState.ACTIVE
        and state.valid_until_utc is not None
    ):
        access_through = format_access_through_date_ru(
            state.valid_until_utc,
            business_timezone,
        )
        lines.extend(
            [
                "",
                f"Текущий VPN продолжает работать до <b>{access_through}</b>.",
            ]
        )
    return "\n".join(lines)
