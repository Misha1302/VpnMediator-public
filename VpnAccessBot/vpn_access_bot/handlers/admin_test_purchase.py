from __future__ import annotations

import logging

from aiogram import Router
from aiogram.filters import Command
from aiogram.types import LabeledPrice, Message

from vpn_access_bot.config import Settings
from vpn_access_bot.constants import (
    ORDER_KIND_PURCHASE,
    PAYMENT_MODE_TELEGRAM_STARS,
)
from vpn_access_bot.db import Database
from vpn_access_bot.mediator_client import MediatorClient
from vpn_access_bot.services import PurchaseService

router = Router(name="admin_test_purchase")
logger = logging.getLogger(__name__)

TEST_PURCHASE_PRICE_STARS = 1


def parse_test_buy_args(text: str | None) -> tuple[int, int] | None:
    parts = (text or "").split()

    if len(parts) != 3:
        return None

    try:
        period_count = int(parts[1])
        max_devices = int(parts[2])
    except ValueError:
        return None

    return period_count, max_devices


@router.message(Command("test_buy"))
async def handle_test_buy(
    message: Message,
    database: Database,
    settings: Settings,
    mediator_client: MediatorClient,
) -> None:
    if message.from_user is None:
        return

    if message.from_user.id not in settings.admin_telegram_ids:
        logger.warning(
            "Unauthorized /test_buy attempt: telegram_id=%s",
            message.from_user.id,
        )
        await message.answer("Эта команда доступна только администратору.")
        return

    if message.chat.type != "private":
        await message.answer("Тестовую покупку можно создавать только в личном чате с ботом.")
        return

    if settings.payment_mode != PAYMENT_MODE_TELEGRAM_STARS:
        await message.answer(
            "Для тестовой покупки должен быть включён <code>PAYMENT_MODE=telegram_stars</code>."
        )
        return

    parsed = parse_test_buy_args(message.text)

    if parsed is None:
        await message.answer(
            "<b>Использование:</b>\n"
            "<code>/test_buy МЕСЯЦЫ УСТРОЙСТВА</code>\n\n"
            "Например:\n"
            "<code>/test_buy 1 1</code>\n"
            "<code>/test_buy 12 12</code>"
        )
        return

    period_count, max_devices = parsed

    try:
        async with database.session() as session:
            service = PurchaseService(
                session,
                settings,
                mediator_client,
            )

            quote = await service.create_quote(
                telegram_id=message.from_user.id,
                username=message.from_user.username,
                first_name=message.from_user.first_name,
                period_count=period_count,
                max_devices=max_devices,
                order_kind=ORDER_KIND_PURCHASE,
            )

            quote.amount_minor_units = TEST_PURCHASE_PRICE_STARS
            quote.final_amount_minor_units = TEST_PURCHASE_PRICE_STARS
            quote.price_before_personal_discount = TEST_PURCHASE_PRICE_STARS
            quote.personal_discount_id = None
            quote.personal_discount_bps = 0
            quote.personal_discount_amount_minor_units = 0
            quote.referral_eligible = False
            quote.is_test_order = True
            quote.trial_claim_id = None
            quote.trial_seconds_remaining_at_quote = 0
            quote.pricing_version = f"{settings.pricing_version}:admin-test"

            await session.flush()

            order = await service.create_order_from_quote(
                quote.public_quote_id,
                message.from_user.id,
            )
            invoice = service.build_telegram_stars_invoice(order)

    except ValueError as exception:
        logger.warning(
            "Admin test purchase rejected: telegram_id=%s periods=%s devices=%s error=%s",
            message.from_user.id,
            period_count,
            max_devices,
            exception,
        )
        await message.answer(
            "Не удалось создать тестовый заказ.\n\nПроверьте количество месяцев и устройств."
        )
        return

    except Exception:
        logger.exception(
            "Failed to create admin test purchase: telegram_id=%s periods=%s devices=%s",
            message.from_user.id,
            period_count,
            max_devices,
        )
        await message.answer("Произошла техническая ошибка при создании тестового заказа.")
        return

    await message.answer(
        "<b>Тестовый заказ создан</b>\n\n"
        f"Номер заказа: <code>{order.public_order_id}</code>\n"
        f"Срок: <b>{period_count} мес.</b>\n"
        f"Устройств: <b>{max_devices}</b>\n"
        f"Цена: <b>{invoice.prices[0].amount} ⭐</b>\n\n"
        "Это настоящий тестовый платёж Telegram Stars."
    )

    try:
        await message.answer_invoice(
            title=f"Тест: {settings.product_name}",
            description=(
                f"Тестовый доступ к {settings.product_name}: "
                f"{period_count} мес., до {max_devices} устройств."
            ),
            payload=invoice.payload,
            provider_token=invoice.provider_token,
            currency=invoice.currency,
            prices=[
                LabeledPrice(
                    label=invoice.prices[0].label,
                    amount=invoice.prices[0].amount,
                ),
            ],
        )
    except Exception:
        logger.exception(
            "Telegram rejected admin test invoice: order_id=%s telegram_id=%s",
            order.public_order_id,
            message.from_user.id,
        )
        await message.answer(
            "Telegram не смог открыть окно оплаты. Деньги не списаны; повторите команду позже."
        )
