from __future__ import annotations

from types import SimpleNamespace
from typing import cast

import pytest

from vpn_access_bot.config import Settings
from vpn_access_bot.constants import PAYMENT_MODE_TELEGRAM_STARS
from vpn_access_bot.handlers.buy import _build_invoice_if_required, handle_buy_menu
from vpn_access_bot.models import Order
from vpn_access_bot.services import PurchaseService, TelegramStarsInvoice


class InvoiceSpy:
    def __init__(self) -> None:
        self.calls = 0

    def build_telegram_stars_invoice(self, order: Order) -> TelegramStarsInvoice:
        self.calls += 1
        raise AssertionError("Complimentary orders must not create an invoice.")


class FakeMessage:
    def __init__(self) -> None:
        self.text = ""
        self.reply_markup = None

    async def edit_text(self, text: str, **kwargs: object) -> None:
        self.text = text
        self.reply_markup = kwargs.get("reply_markup")


class FakeCallback:
    def __init__(self) -> None:
        self.message = FakeMessage()
        self.answer_count = 0

    async def answer(self) -> None:
        self.answer_count += 1


class ReadyCommerce:
    async def check(self, **kwargs: object):
        _ = kwargs
        return SimpleNamespace(can_sell=True)


def test_complimentary_order_skips_telegram_stars_invoice() -> None:
    service = InvoiceSpy()
    order = Order(amount_minor_units=0)
    settings_stub_type = type(
        "SettingsStub",
        (),
        {"payment_mode": PAYMENT_MODE_TELEGRAM_STARS},
    )
    settings = cast(Settings, settings_stub_type())

    invoice = _build_invoice_if_required(
        cast(PurchaseService, service),
        order,
        settings,
    )

    assert invoice is None
    assert service.calls == 0


@pytest.mark.asyncio
async def test_buy_menu_selects_device_count_and_period_in_one_step() -> None:
    callback = FakeCallback()
    settings = Settings(
        TELEGRAM_BOT_TOKEN="test-token",
        MEDIATOR_ADMIN_TOKEN="test-admin-token",
    )

    await handle_buy_menu(
        callback,  # type: ignore[arg-type]
        settings,
        ReadyCommerce(),  # type: ignore[arg-type]
    )

    assert callback.answer_count == 1
    assert "количество устройств и срок" in callback.message.text
    callbacks = {
        button.callback_data
        for row in callback.message.reply_markup.inline_keyboard
        for button in row
        if button.callback_data is not None
    }
    assert "buy:period:1:1" in callbacks
    assert "buy:period:12:12" in callbacks
    assert not any(value.startswith("buy:devices:") for value in callbacks)
