from vpn_access_bot.client_catalog import ClientAppCatalog, Platform
from vpn_access_bot.commerce import (
    CabinetState,
    CabinetSubscriptionState,
    OrderNoticeState,
)
from vpn_access_bot.config import Settings
from vpn_access_bot.constants import ORDER_KIND_PURCHASE
from vpn_access_bot.handlers.subscription import _visible_devices
from vpn_access_bot.keyboards import (
    after_purchase_keyboard,
    device_limit_keyboard,
    devices_keyboard,
    main_menu_keyboard,
    more_menu_keyboard,
    onboarding_access_keyboard,
    onboarding_install_keyboard,
    other_platforms_keyboard,
    platform_selection_keyboard,
    purchase_devices_keyboard,
    purchase_other_devices_keyboard,
    purchase_other_packages_keyboard,
    purchase_packages_keyboard,
    purchase_periods_keyboard,
    quote_confirmation_keyboard,
    subscription_keyboard,
    upgrade_devices_keyboard,
    upgrade_other_devices_keyboard,
)
from vpn_access_bot.product_catalog import ProductCatalog


def _callback_data(markup) -> set[str]:
    return {
        button.callback_data
        for row in markup.inline_keyboard
        for button in row
        if button.callback_data is not None
    }


def test_active_main_menu_opens_shared_subscription_without_device_selection() -> None:
    keyboard = main_menu_keyboard(
        CabinetState(
            subscription_state=CabinetSubscriptionState.ACTIVE,
            order_notice_state=OrderNoticeState.NONE,
            valid_until_utc=None,
            remaining_days=10,
            active_device_tokens=1,
            max_device_tokens=3,
            mediator_available=True,
            commerce_available=True,
            trial_available=False,
            trial_retry_available=False,
            pending_order_public_id=None,
            pending_order_kind=None,
        )
    )

    callback_data = _callback_data(keyboard)

    first_button = keyboard.inline_keyboard[0][0]
    assert first_button.text == "Открыть в Happ"
    assert first_button.callback_data == "credential:create"
    assert "subscription:link" not in callback_data
    assert "onboarding:install" not in callback_data
    assert "device:add" not in callback_data


def test_device_limit_keyboard_uses_registered_subscription_callbacks() -> None:
    keyboard = device_limit_keyboard()

    callback_data = _callback_data(keyboard)

    assert callback_data == {"subscription:devices", "menu:main"}


def test_unavailable_commerce_does_not_offer_payment_or_trial() -> None:
    keyboard = main_menu_keyboard(
        CabinetState(
            subscription_state=CabinetSubscriptionState.NONE,
            order_notice_state=OrderNoticeState.NONE,
            valid_until_utc=None,
            remaining_days=0,
            active_device_tokens=0,
            max_device_tokens=None,
            mediator_available=True,
            commerce_available=False,
            trial_available=True,
            trial_retry_available=False,
            pending_order_public_id=None,
            pending_order_kind=None,
        )
    )

    callback_data = _callback_data(keyboard)
    button_texts = {button.text for row in keyboard.inline_keyboard for button in row}

    assert "trial:show" not in callback_data
    assert "Проверить доступность" in button_texts


def test_unknown_saved_platform_does_not_break_platform_keyboard() -> None:
    from vpn_access_bot.keyboards import platform_selection_keyboard

    keyboard = platform_selection_keyboard("legacy-unknown-platform")

    assert "onboarding:platform:android" in _callback_data(keyboard)


def test_onboarding_access_keyboard_exposes_explicit_credential_action() -> None:
    keyboard = onboarding_access_keyboard("Razaltush VPN")

    assert keyboard.inline_keyboard[0][0].text == "Открыть в Happ"
    assert keyboard.inline_keyboard[0][0].callback_data == "credential:create"
    assert "onboarding:install" in _callback_data(keyboard)


def _state(
    *,
    subscription_state=CabinetSubscriptionState.NONE,
    order_notice_state=OrderNoticeState.NONE,
    pending_order_public_id=None,
    trial_available=False,
):
    return CabinetState(
        subscription_state=subscription_state,
        order_notice_state=order_notice_state,
        valid_until_utc=None,
        remaining_days=0,
        active_device_tokens=0,
        max_device_tokens=3,
        mediator_available=True,
        commerce_available=True,
        trial_available=trial_available,
        trial_retry_available=False,
        pending_order_public_id=pending_order_public_id,
        pending_order_kind="purchase" if pending_order_public_id else None,
    )


def test_pending_payment_menu_isolated_from_new_purchase() -> None:
    keyboard = main_menu_keyboard(
        _state(
            order_notice_state=OrderNoticeState.PAYMENT_PENDING,
            pending_order_public_id="order-1",
            trial_available=True,
        )
    )

    callback_data = _callback_data(keyboard)
    assert callback_data == {
        "order:continue:order-1",
        "order:cancel:order-1",
        "order:payment_help:order-1",
    }
    assert "buy:menu" not in callback_data
    assert "trial:show" not in callback_data


def test_activation_failed_menu_does_not_offer_new_purchase() -> None:
    keyboard = main_menu_keyboard(
        _state(
            subscription_state=CabinetSubscriptionState.ACTIVE,
            order_notice_state=OrderNoticeState.ACTIVATION_FAILED,
            pending_order_public_id="order-2",
        )
    )

    callback_data = _callback_data(keyboard)
    assert callback_data == {
        "order:retry:order-2",
        "order:payment_help:order-2",
    }
    assert "buy:menu" not in callback_data


def test_payment_received_menu_only_offers_activation_status_and_help() -> None:
    keyboard = main_menu_keyboard(
        _state(
            subscription_state=CabinetSubscriptionState.ACTIVE,
            order_notice_state=OrderNoticeState.PAYMENT_RECEIVED,
            pending_order_public_id="order-3",
        )
    )

    callback_data = _callback_data(keyboard)
    assert callback_data == {"menu:main", "order:payment_help:order-3"}
    assert "buy:menu" not in callback_data
    assert "buy:renew" not in callback_data


def test_active_main_menu_exposes_renew_and_more_without_duplicate_subscription_button() -> None:
    keyboard = main_menu_keyboard(_state(subscription_state=CabinetSubscriptionState.ACTIVE))

    callback_data = _callback_data(keyboard)
    assert {"credential:create", "subscription:devices", "buy:renew", "menu:more"} <= callback_data
    assert "subscription:show" not in callback_data
    assert "support:show" not in callback_data


def test_primary_subscription_keyboards_never_require_platform_selection() -> None:
    keyboards = [
        subscription_keyboard(True),
        after_purchase_keyboard(),
        devices_keyboard([]),
    ]

    for keyboard in keyboards:
        callbacks = _callback_data(keyboard)
        assert "credential:create" in callbacks
        assert "subscription:link" not in callbacks
        assert "onboarding:any" not in callbacks
        assert keyboard.inline_keyboard[0][0].text == "Открыть в Happ"


def test_platform_selection_is_only_an_optional_install_helper() -> None:
    keyboard = after_purchase_keyboard()
    callbacks = _callback_data(keyboard)

    assert "credential:create" in callbacks
    assert "onboarding:install" in callbacks
    assert keyboard.inline_keyboard[0][0].text == "Открыть в Happ"
    assert keyboard.inline_keyboard[1][0].text == "Установить Happ"


def test_purchase_device_menu_uses_compact_featured_options_and_progressive_disclosure() -> None:
    keyboard = purchase_devices_keyboard(tuple(range(1, 13)))
    labels = [button.text for row in keyboard.inline_keyboard for button in row]

    assert "1 устройство" in labels
    assert "2 устройства" in labels
    assert "6 устройств" in labels
    assert "12 устройств" in labels
    assert "Другое количество" in labels
    assert "3 устройства" not in labels
    assert len(keyboard.inline_keyboard[0]) == 2
    assert len(keyboard.inline_keyboard[1]) == 2


def test_other_device_menu_contains_remaining_values_in_two_columns() -> None:
    keyboard = purchase_other_devices_keyboard(tuple(range(1, 13)))
    labels = [button.text for row in keyboard.inline_keyboard for button in row]

    assert {"3 устройства", "4 устройства", "5 устройств", "11 устройств"} <= set(labels)
    assert {"1 устройство", "2 устройства", "6 устройств", "12 устройств"}.isdisjoint(labels)
    assert all(len(row) <= 2 for row in keyboard.inline_keyboard)


def test_purchase_package_menu_selects_devices_and_period_in_one_callback() -> None:
    settings = Settings(
        TELEGRAM_BOT_TOKEN="test-token",
        MEDIATOR_ADMIN_TOKEN="test-admin-token",
    )
    catalog = ProductCatalog.from_settings(settings)

    keyboard = purchase_packages_keyboard(catalog)
    callbacks = _callback_data(keyboard)
    labels = [button.text for row in keyboard.inline_keyboard for button in row]

    assert "buy:period:1:1" in callbacks
    assert "buy:period:12:12" in callbacks
    assert "buy:packages_other" in callbacks
    assert not any(callback.startswith("buy:devices:") for callback in callbacks)
    assert "1×30 дн. · 60 ⭐" in labels
    assert "12×360 дн. · 6048 ⭐" in labels
    assert all(len(row) <= 2 for row in keyboard.inline_keyboard)


def test_other_purchase_package_menu_contains_only_non_featured_device_values() -> None:
    settings = Settings(
        TELEGRAM_BOT_TOKEN="test-token",
        MEDIATOR_ADMIN_TOKEN="test-admin-token",
    )
    catalog = ProductCatalog.from_settings(settings)

    keyboard = purchase_other_packages_keyboard(catalog)
    callbacks = _callback_data(keyboard)

    assert "buy:period:3:1" in callbacks
    assert "buy:period:11:12" in callbacks
    assert "buy:period:1:1" not in callbacks
    assert "buy:period:2:1" not in callbacks
    assert "buy:period:6:1" not in callbacks
    assert "buy:period:12:1" not in callbacks
    assert all(len(callback.encode("utf-8")) <= 64 for callback in callbacks)
    assert all(len(row) <= 2 for row in keyboard.inline_keyboard)


def test_quote_confirmation_opens_payment_without_legal_document_step() -> None:
    keyboard = quote_confirmation_keyboard("quote-1")

    assert keyboard.inline_keyboard[0][0].text == "Оплатить звёздами"
    assert keyboard.inline_keyboard[0][0].callback_data == "buy:pay:quote-1"
    assert all(button.url is None for row in keyboard.inline_keyboard for button in row)


def test_quote_confirmation_offers_stars_and_sbp_as_separate_actions() -> None:
    keyboard = quote_confirmation_keyboard(
        "quote-1",
        stars_amount=60,
        sbp_url="https://pay.example.test/checkout/token",
        sbp_amount_minor_units=19900,
    )

    assert keyboard.inline_keyboard[0][0].text == "Оплатить звёздами — 60 ⭐"
    assert keyboard.inline_keyboard[0][0].callback_data == "buy:pay:quote-1"
    assert keyboard.inline_keyboard[1][0].text == "Оплатить по СБП — 199 ₽"
    assert keyboard.inline_keyboard[1][0].url == "https://pay.example.test/checkout/token"


def test_period_menu_uses_catalog_prices_instead_of_hardcoded_text() -> None:
    settings = Settings(
        TELEGRAM_BOT_TOKEN="test-token",
        MEDIATOR_ADMIN_TOKEN="test-admin-token",
    )
    catalog = ProductCatalog.from_settings(settings)
    keyboard = purchase_periods_keyboard(
        catalog,
        selected_devices=1,
        price_devices=1,
        operation_kind=ORDER_KIND_PURCHASE,
    )
    labels = [row[0].text for row in keyboard.inline_keyboard[:-1]]

    assert labels == [
        "30 дней — 60 ⭐",
        "90 дней — 162 ⭐ · экономия 10%",
        "180 дней — 288 ⭐ · экономия 20%",
        "360 дней — 504 ⭐ · выгоднее всего · −30%",
    ]


def test_primary_platform_menu_hides_advanced_platforms() -> None:
    keyboard = platform_selection_keyboard()
    callback_data = _callback_data(keyboard)

    assert "onboarding:platform:linux" not in callback_data
    assert "onboarding:router" not in callback_data
    assert "onboarding:other" in callback_data

    advanced = _callback_data(other_platforms_keyboard())
    assert {"onboarding:platform:linux", "onboarding:router"} <= advanced


def test_install_screen_combines_app_installation_and_credential_creation() -> None:
    platform = ClientAppCatalog.default().get(Platform.ANDROID)
    keyboard = onboarding_install_keyboard(platform)
    callback_data = _callback_data(keyboard)

    assert "credential:create" in callback_data
    assert "onboarding:installed" not in callback_data


def test_list_price_rejects_device_limit_outside_catalog() -> None:
    settings = Settings(
        TELEGRAM_BOT_TOKEN="test-token",
        MEDIATOR_ADMIN_TOKEN="test-admin-token",
    )
    catalog = ProductCatalog.from_settings(settings)

    import pytest

    with pytest.raises(ValueError, match="unsupported_device_option"):
        catalog.calculate_list_price(1, 999)


def test_renewal_price_menu_supports_grandfathered_device_limit() -> None:
    settings = Settings(
        TELEGRAM_BOT_TOKEN="test-token",
        MEDIATOR_ADMIN_TOKEN="test-admin-token",
    )
    catalog = ProductCatalog.from_settings(settings)

    keyboard = purchase_periods_keyboard(
        catalog,
        selected_devices=None,
        price_devices=6,
        operation_kind="extend",
        grandfathered_device_limit=6,
    )

    assert keyboard.inline_keyboard[0][0].text == "30 дней — 360 ⭐"


def test_upgrade_device_menu_uses_featured_values_and_hides_the_rest() -> None:
    keyboard = upgrade_devices_keyboard(1, tuple(range(1, 13)))
    labels = [button.text for row in keyboard.inline_keyboard for button in row]

    assert {"До 2 устройств", "До 6 устройств", "До 12 устройств"} <= set(labels)
    assert "Другое количество" in labels
    assert "До 3 устройств" not in labels

    other = upgrade_other_devices_keyboard(1, tuple(range(1, 13)))
    other_labels = [button.text for row in other.inline_keyboard for button in row]
    assert "До 3 устройств" in other_labels
    assert "До 6 устройств" not in other_labels


def test_more_menu_keeps_only_actionable_secondary_items() -> None:
    keyboard = more_menu_keyboard()
    callbacks = _callback_data(keyboard)

    assert callbacks == {"buy:upgrade", "referral:show", "support:show", "menu:main"}
    assert "about:show" not in callbacks


def test_device_management_keeps_only_disable_or_enable_actions() -> None:
    from vpn_access_bot.keyboards import device_management_keyboard

    active_keyboard = device_management_keyboard("device-1", can_transfer=True)
    disabled_keyboard = device_management_keyboard("device-1", disabled=True)

    assert _callback_data(active_keyboard) == {
        "device:revoke:device-1",
        "subscription:devices",
    }
    assert _callback_data(disabled_keyboard) == {
        "device:enable:device-1",
        "subscription:devices",
    }


def test_device_list_keeps_occupied_legacy_rows_and_hides_only_revoked_legacy_rows() -> None:
    from types import SimpleNamespace

    devices = [
        SimpleNamespace(access_channel="device_link", state="active", public_id="legacy-active"),
        SimpleNamespace(access_channel="device_link", state="pending", public_id="legacy-pending"),
        SimpleNamespace(access_channel="device_link", state="revoked", public_id="legacy-revoked"),
        SimpleNamespace(
            access_channel="unified_feed", state="revoked", public_id="unified-disabled"
        ),
    ]

    visible = _visible_devices(devices)

    assert [device.public_id for device in visible] == [
        "legacy-active",
        "legacy-pending",
        "unified-disabled",
    ]
