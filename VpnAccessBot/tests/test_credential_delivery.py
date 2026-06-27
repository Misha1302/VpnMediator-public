from __future__ import annotations

from urllib.parse import parse_qs, urlsplit

import pytest

from vpn_access_bot.credential_delivery import COPY_TEXT_MAX_LENGTH, build_delivery_plan
from vpn_access_bot.keyboards import credential_delivery_keyboard


def test_copy_flow_is_primary_when_deep_link_is_not_configured() -> None:
    url = "https://vpn.example/sub/owner/devices/device/servers.txt?token=fake-secret"

    plan = build_delivery_plan(url, happ_deep_link_template=None)
    keyboard = credential_delivery_keyboard(
        plan.connection_url,
        happ_deep_link=plan.happ_deep_link,
        can_copy=plan.can_copy,
    )

    assert plan.happ_deep_link is None
    assert plan.can_copy is True
    copy_button = keyboard.inline_keyboard[0][0]
    assert copy_button.text == "Скопировать ссылку"
    assert copy_button.copy_text is not None
    assert copy_button.copy_text.text == url
    assert all(button.url is None for row in keyboard.inline_keyboard for button in row)


def test_verified_template_encodes_subscription_url_once() -> None:
    url = "https://vpn.example/sub/id/servers.txt?token=fake-secret&x=1"

    plan = build_delivery_plan(
        url,
        happ_deep_link_template="happ://add?url={url}",
    )

    assert plan.happ_deep_link is not None
    parsed = urlsplit(plan.happ_deep_link)
    assert parsed.scheme == "happ"
    assert parse_qs(parsed.query)["url"] == [url]


@pytest.mark.parametrize(
    "template",
    [
        "https://example.test/add?url={url}",
        "javascript:{url}",
        "happ://add",
        "happ://add?first={url}&second={url}",
    ],
)
def test_unverified_or_invalid_deep_link_templates_are_rejected(template: str) -> None:
    with pytest.raises(ValueError):
        build_delivery_plan(
            "https://vpn.example/sub/id/servers.txt?token=fake-secret",
            happ_deep_link_template=template,
        )


def test_long_url_disables_copy_text_button() -> None:
    url = "https://vpn.example/sub/id/servers.txt?token=" + "x" * COPY_TEXT_MAX_LENGTH

    plan = build_delivery_plan(url, happ_deep_link_template=None)
    keyboard = credential_delivery_keyboard(
        plan.connection_url,
        happ_deep_link=None,
        can_copy=plan.can_copy,
    )

    assert plan.can_copy is False
    assert all(button.copy_text is None for row in keyboard.inline_keyboard for button in row)


def test_fallback_url_preserves_path_query_and_secret() -> None:
    plan = build_delivery_plan(
        "https://vpn.example/base/sub/id/servers.txt?token=fake-secret",
        happ_deep_link_template=None,
        primary_subscription_base_url="https://vpn.example/base",
        fallback_subscription_base_url="https://vpn-fallback.example/edge",
    )

    assert plan.fallback_connection_url == (
        "https://vpn-fallback.example/edge/sub/id/servers.txt?token=fake-secret"
    )


def test_fallback_url_rejects_unrelated_primary_url() -> None:
    with pytest.raises(ValueError):
        build_delivery_plan(
            "https://attacker.example/sub/id?token=fake-secret",
            happ_deep_link_template=None,
            primary_subscription_base_url="https://vpn.example",
            fallback_subscription_base_url="https://vpn-fallback.example",
        )


def test_credential_keyboard_uses_manual_check_only_as_diagnostic_fallback() -> None:
    url = "https://vpn.example/sub/id/servers.txt?token=fake-secret"
    keyboard = credential_delivery_keyboard(url, happ_deep_link=None, can_copy=True)

    labels = [button.text for row in keyboard.inline_keyboard for button in row]
    callbacks = {
        button.callback_data
        for row in keyboard.inline_keyboard
        for button in row
        if button.callback_data is not None
    }

    assert "Не появилось в Happ?" in labels
    assert "Проверить получение" not in labels
    assert "Удалить сообщение со ссылкой" not in labels
    assert "credential:check" in callbacks
    assert "credential:delete_message" not in callbacks
