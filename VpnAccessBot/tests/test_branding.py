from __future__ import annotations

import inspect

import pytest
from pydantic import ValidationError

import vpn_access_bot.handlers.subscription as subscription_handlers
from vpn_access_bot.commerce import CabinetStateBuilder
from vpn_access_bot.config import Settings
from vpn_access_bot.texts import happ_instruction_text, support_text, welcome_text
from vpn_access_bot.trial import TrialEligibility, TrialEligibilityReason
from vpn_access_bot.user_texts import cabinet_text


def make_settings(**overrides: str) -> Settings:
    values = {
        "TELEGRAM_BOT_TOKEN": "test-token",
        "MEDIATOR_ADMIN_TOKEN": "test-admin-token",
    }
    values.update(overrides)
    return Settings(**values)


def test_product_name_defaults_to_razaltush_vpn() -> None:
    settings = make_settings()

    assert settings.product_name == "Razaltush VPN"


def test_product_name_is_trimmed_and_validated() -> None:
    settings = make_settings(PRODUCT_NAME="  Custom VPN  ")

    assert settings.product_name == "Custom VPN"

    with pytest.raises(ValidationError):
        make_settings(PRODUCT_NAME=" ")


def test_user_facing_branding_uses_configured_product_name() -> None:
    product_name = "Razaltush VPN"
    state = CabinetStateBuilder().build(
        None,
        None,
        TrialEligibility(True, False, TrialEligibilityReason.AVAILABLE),
    )

    assert product_name in welcome_text(product_name)
    assert product_name in happ_instruction_text(product_name)
    assert product_name in support_text(product_name)
    assert product_name in cabinet_text(state, product_name)


def test_shared_catalog_messages_do_not_promise_raw_credential_revocation() -> None:
    source = inspect.getsource(subscription_handlers)

    assert "VPN на этом устройстве будет отключён" not in source
    assert "Старая ссылка этого устройства сразу перестанет работать" not in source
    assert "Все текущие подключения перестанут работать" not in source
