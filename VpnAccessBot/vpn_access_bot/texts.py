from __future__ import annotations

from datetime import datetime

from vpn_access_bot.formatting import format_datetime_ru
from vpn_access_bot.keyboards import format_price
from vpn_access_bot.models import Order, Tariff


def format_datetime(value: datetime) -> str:
    return format_datetime_ru(value)


def welcome_text(product_name: str) -> str:
    return (
        f"<b>{product_name}</b>\n\n"
        "Простой VPN для телефона и компьютера.\n"
        "Можно попробовать 2 дня бесплатно без карты или сразу выбрать подходящий срок."
    )


def tariff_text(tariff: Tariff) -> str:
    return (
        f"<b>{tariff.title}</b>\n"
        f"{tariff.description}\n"
        f"Цена: <b>{format_price(tariff.price_minor_units, tariff.currency)}</b>\n"
        f"Устройств: <b>{tariff.max_devices}</b>\n"
        f"Срок: <b>{tariff.duration_days} дней</b>"
    )


def happ_instruction_text(product_name: str) -> str:
    return (
        f"<b>Как подключить {product_name}</b>\n\n"
        "1. Активируйте бесплатный период или оплатите доступ.\n"
        "2. Нажмите «Открыть в Happ».\n"
        "3. Если Happ ещё не установлен, используйте кнопку «Установить Happ».\n"
        "4. Добавьте подписку и включите VPN большой кнопкой.\n\n"
        "Одна и та же ссылка работает на всех оплаченных устройствах. "
        "Количество устройств определяется автоматически по установкам Happ."
    )


def support_text(product_name: str) -> str:
    return (
        f"<b>Помощь {product_name}</b>\n\n"
        "Опишите, на каком шаге возникла проблема. Для диагностики укажите модель "
        "устройства, платформу, время последней попытки и приложите скриншот ошибки.\n\n"
        "Не отправляйте ссылку подключения, коды доступа или другие секретные данные."
    )


def _order_status_text(status: str) -> str:
    return {
        "pending": "ожидает оплаты",
        "payment_received": "оплата получена",
        "activating": "доступ активируется",
        "paid": "доступ активирован",
        "activation_failed": "нужна повторная активация",
        "refunded": "выполнен возврат",
        "expired": "заказ истёк",
        "failed": "операция завершилась ошибкой",
    }.get(status, "проверяется")


def pay_support_text(order: Order | None) -> str:
    order_line = ""

    if order is not None:
        order_line = (
            f"\n\nЗаказ: <code>{order.public_order_id}</code>\n"
            f"Состояние: <b>{_order_status_text(order.status)}</b>"
        )

    return (
        "<b>Помощь с оплатой</b>\n\n"
        "Если Stars списались, но доступ не появился, не оплачивайте заказ повторно. "
        "Мы проверим уже полученный платёж и безопасно повторим активацию."
        f"{order_line}\n\n"
        "Сохраните чек Telegram. При обращении достаточно указать номер заказа; "
        "ссылку подключения присылать не нужно."
    )
