from pathlib import Path

import pytest
from pydantic import ValidationError

from vpn_access_bot.config import Settings
from vpn_access_bot.runtime import SingleInstanceGuard


def base_values(**overrides: object) -> dict[str, object]:
    values: dict[str, object] = {
        "TELEGRAM_BOT_TOKEN": "123456789:" + "A" * 40,
        "MEDIATOR_ADMIN_TOKEN": "m" * 40,
        "APP_ENV": "production",
        "ADMIN_TELEGRAM_IDS": "1",
        "SUPPORT_AGENT_TELEGRAM_IDS": "2",
        "SUPPORT_CHAT_ID": "-1001234567890",
        "PAYMENT_MODE": "telegram_stars",
        "DATABASE_URL": "sqlite+aiosqlite:////var/lib/vpn-access-bot/bot.db",
        "PUBLIC_SUBSCRIPTION_BASE_URL": "https://vpn.example.test",
        "SUPPORT_CONTACT": "@support",
    }
    values.update(overrides)
    return values


def test_production_settings_accept_complete_configuration() -> None:
    settings = Settings(**base_values())
    assert settings.app_env == "production"


def test_production_settings_accept_explicit_yookassa_configuration() -> None:
    settings = Settings(
        **base_values(
            EXTERNAL_PAYMENT_ENABLED=True,
            YOOKASSA_INTEGRATION_ENABLED=True,
            CHECKOUT_PUBLIC_BASE_URL="https://pay.example.test",
            CHECKOUT_TOKEN_SECRET="c" * 40,
            YOOKASSA_SHOP_ID="123456",
            YOOKASSA_SECRET_KEY="y" * 40,
            YOOKASSA_RETURN_URL="https://pay.example.test/payment/return",
            YOOKASSA_WEBHOOK_PATH_SECRET="w" * 32,
        )
    )
    assert settings.external_payment_enabled is True


def test_integration_can_remain_enabled_while_new_sbp_sales_are_disabled() -> None:
    settings = Settings(
        **base_values(
            YOOKASSA_INTEGRATION_ENABLED=True,
            EXTERNAL_PAYMENT_ENABLED=False,
            CHECKOUT_PUBLIC_BASE_URL="https://pay.example.test",
            CHECKOUT_TOKEN_SECRET="c" * 40,
            YOOKASSA_SHOP_ID="123456",
            YOOKASSA_SECRET_KEY="y" * 40,
            YOOKASSA_RETURN_URL="https://pay.example.test/payment/return",
            YOOKASSA_WEBHOOK_PATH_SECRET="w" * 32,
        )
    )
    assert settings.yookassa_integration_enabled is True
    assert settings.external_payment_enabled is False


def test_external_payment_rejects_missing_checkout_secret() -> None:
    with pytest.raises(ValidationError):
        Settings(
            **base_values(
                EXTERNAL_PAYMENT_ENABLED=True,
                YOOKASSA_INTEGRATION_ENABLED=True,
                CHECKOUT_PUBLIC_BASE_URL="https://pay.example.test",
                YOOKASSA_SHOP_ID="123456",
                YOOKASSA_SECRET_KEY="y" * 40,
                YOOKASSA_RETURN_URL="https://pay.example.test/payment/return",
                YOOKASSA_WEBHOOK_PATH_SECRET="w" * 32,
            )
        )


def test_external_button_requires_recovery_listener() -> None:
    with pytest.raises(ValidationError, match="YOOKASSA_INTEGRATION_ENABLED"):
        Settings(
            TELEGRAM_BOT_TOKEN="test-token",
            MEDIATOR_ADMIN_TOKEN="test-admin-token",
            EXTERNAL_PAYMENT_ENABLED=True,
        )


@pytest.mark.parametrize(
    "api_url",
    [
        "https://attacker.example/v3",
        "https://api.yookassa.ru:8443/v3",
        "https://user@api.yookassa.ru/v3",
        "https://api.yookassa.ru/v3?redirect=1",
    ],
)
def test_production_rejects_noncanonical_yookassa_api_url(api_url: str) -> None:
    with pytest.raises(ValidationError):
        Settings(
            **base_values(
                YOOKASSA_INTEGRATION_ENABLED=True,
                EXTERNAL_PAYMENT_ENABLED=True,
                CHECKOUT_PUBLIC_BASE_URL="https://pay.example.test",
                CHECKOUT_TOKEN_SECRET="c" * 40,
                YOOKASSA_SHOP_ID="123456",
                YOOKASSA_SECRET_KEY="y" * 40,
                YOOKASSA_API_BASE_URL=api_url,
                YOOKASSA_RETURN_URL="https://pay.example.test/payment/return",
                YOOKASSA_WEBHOOK_PATH_SECRET="w" * 32,
            )
        )


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("PAYMENT_MODE", "manual"),
        ("PUBLIC_SUBSCRIPTION_BASE_URL", "http://vpn.example.test"),
        ("ADMIN_TELEGRAM_IDS", ""),
        ("SUPPORT_AGENT_TELEGRAM_IDS", ""),
        ("SUPPORT_CHAT_ID", None),
        ("DATABASE_URL", "sqlite+aiosqlite:///./data/vpn_bot.db"),
        ("MEDIATOR_ADMIN_TOKEN", "change-me"),
    ],
)
def test_production_settings_reject_unsafe_configuration(field: str, value: object) -> None:
    with pytest.raises(ValidationError):
        Settings(**base_values(**{field: value}))


def test_single_instance_guard_rejects_second_writer(tmp_path: Path) -> None:
    path = tmp_path / "bot.lock"
    first = SingleInstanceGuard(str(path))
    second = SingleInstanceGuard(str(path))
    first.acquire()
    try:
        with pytest.raises(RuntimeError, match="already owns"):
            second.acquire()
    finally:
        first.release()
