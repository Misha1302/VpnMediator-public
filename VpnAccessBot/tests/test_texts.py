from datetime import UTC, datetime

from vpn_access_bot.commerce import CabinetState, CabinetSubscriptionState, OrderNoticeState
from vpn_access_bot.texts import format_datetime, happ_instruction_text
from vpn_access_bot.user_texts import cabinet_text


def test_format_datetime_uses_russian_date() -> None:
    value = datetime(2026, 6, 1, 12, 0, tzinfo=UTC)

    assert format_datetime(value) == "1 июня, 15:00 МСК"


def _cabinet_state(order_notice_state: OrderNoticeState) -> CabinetState:
    return CabinetState(
        subscription_state=CabinetSubscriptionState.NONE,
        order_notice_state=order_notice_state,
        valid_until_utc=None,
        remaining_days=None,
        active_device_tokens=0,
        max_device_tokens=0,
        mediator_available=True,
        commerce_available=True,
        trial_available=True,
        trial_retry_available=False,
        pending_order_public_id="order-1",
        pending_order_kind="purchase",
    )


def test_pending_order_text_replaces_regular_purchase_screen() -> None:
    text = cabinet_text(_cabinet_state(OrderNoticeState.PAYMENT_PENDING), "Razaltush VPN")

    assert "Оплата ещё не завершена" in text
    assert "Новую покупку создавать не нужно" in text
    assert "2 дня бесплатно" not in text


def test_activation_failure_text_says_payment_is_preserved() -> None:
    text = cabinet_text(_cabinet_state(OrderNoticeState.ACTIVATION_FAILED), "Razaltush VPN")

    assert "Оплата сохранена" in text
    assert "Повторно платить не нужно" in text


def test_happ_instruction_matches_direct_credential_delivery() -> None:
    text = happ_instruction_text("Razaltush VPN")

    assert "Открыть в Happ" in text
    assert "одна и та же ссылка" in text.lower()
    assert "Подключить устройство" not in text
    assert "выберите платформу" not in text.lower()
    assert "временной странице" not in text
    assert "действует несколько минут" not in text


def test_server_count_is_hidden_when_not_verified() -> None:
    state = _cabinet_state(OrderNoticeState.NONE)

    assert "проверенн" not in cabinet_text(state, "Razaltush VPN")
