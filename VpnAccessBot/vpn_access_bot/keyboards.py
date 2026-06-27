from __future__ import annotations

from aiogram.types import CopyTextButton, InlineKeyboardButton, InlineKeyboardMarkup

from vpn_access_bot.client_catalog import ClientAppPlatform, Platform
from vpn_access_bot.commerce import (
    CabinetState,
    OrderNoticeState,
    SubscriptionLifecycleState,
)
from vpn_access_bot.formatting import days_ru
from vpn_access_bot.models import Tariff
from vpn_access_bot.product_catalog import ProductCatalog


def main_menu_keyboard(state: CabinetState) -> InlineKeyboardMarkup:
    if state.order_notice_state != OrderNoticeState.NONE:
        return pending_order_keyboard(state)

    rows: list[list[InlineKeyboardButton]] = []

    if state.subscription_state == SubscriptionLifecycleState.ACTIVE:
        rows.extend(
            [
                [InlineKeyboardButton(text="Открыть в Happ", callback_data="credential:create")],
                [InlineKeyboardButton(text="Мои устройства", callback_data="subscription:devices")],
                [InlineKeyboardButton(text="Продлить доступ", callback_data="buy:renew")],
                [InlineKeyboardButton(text="Ещё", callback_data="menu:more")],
            ]
        )
    elif state.subscription_state == SubscriptionLifecycleState.EXPIRED:
        rows.extend(
            [
                [InlineKeyboardButton(text="Возобновить доступ", callback_data="buy:resume")],
                [InlineKeyboardButton(text="Мои устройства", callback_data="subscription:devices")],
                [InlineKeyboardButton(text="Помощь", callback_data="support:show")],
            ]
        )
    elif state.subscription_state == SubscriptionLifecycleState.DISABLED:
        rows.extend(
            [
                [InlineKeyboardButton(text="Мой доступ", callback_data="subscription:show")],
                [InlineKeyboardButton(text="Помощь", callback_data="support:show")],
            ]
        )
    else:
        if state.commerce_available:
            if state.trial_available:
                rows.append(
                    [
                        InlineKeyboardButton(
                            text="Попробовать 2 дня бесплатно", callback_data="trial:show"
                        )
                    ]
                )
            elif state.trial_retry_available:
                rows.append(
                    [
                        InlineKeyboardButton(
                            text="Повторить включение доступа", callback_data="trial:activate"
                        )
                    ]
                )
            rows.append([InlineKeyboardButton(text="Купить подписку", callback_data="buy:menu")])
        else:
            rows.append(
                [InlineKeyboardButton(text="Проверить доступность", callback_data="buy:menu")]
            )
        rows.extend(
            [
                [InlineKeyboardButton(text="Как подключается VPN", callback_data="help:happ")],
                [InlineKeyboardButton(text="Помощь", callback_data="support:show")],
            ]
        )

    return InlineKeyboardMarkup(inline_keyboard=rows)


def pending_order_keyboard(state: CabinetState) -> InlineKeyboardMarkup:
    order_id = state.pending_order_public_id
    if order_id is None or state.order_notice_state == OrderNoticeState.NONE:
        raise ValueError("Pending order keyboard requires an unfinished order.")

    if state.order_notice_state == OrderNoticeState.PAYMENT_PENDING:
        rows = [
            [
                InlineKeyboardButton(
                    text="Продолжить оплату",
                    callback_data=f"order:continue:{order_id}",
                )
            ],
            [
                InlineKeyboardButton(
                    text="Отменить заказ",
                    callback_data=f"order:cancel:{order_id}",
                )
            ],
            [
                InlineKeyboardButton(
                    text="Помощь с оплатой",
                    callback_data=f"order:payment_help:{order_id}",
                )
            ],
        ]
    elif state.order_notice_state == OrderNoticeState.ACTIVATION_FAILED:
        rows = [
            [
                InlineKeyboardButton(
                    text="Повторить активацию",
                    callback_data=f"order:retry:{order_id}",
                )
            ],
            [
                InlineKeyboardButton(
                    text="Проблема не решилась",
                    callback_data=f"order:payment_help:{order_id}",
                )
            ],
        ]
    else:
        rows = [
            [InlineKeyboardButton(text="Проверить активацию", callback_data="menu:main")],
            [
                InlineKeyboardButton(
                    text="Помощь",
                    callback_data=f"order:payment_help:{order_id}",
                )
            ],
        ]

    return InlineKeyboardMarkup(inline_keyboard=rows)


def back_to_main_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[[InlineKeyboardButton(text="🏠 Главное меню", callback_data="menu:main")]]
    )


def payment_help_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="Написать в поддержку",
                    callback_data="support:cat:payment",
                )
            ],
            [InlineKeyboardButton(text="🏠 Главное меню", callback_data="menu:main")],
        ]
    )


def continue_sbp_keyboard(checkout_url: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="Продолжить оплату по СБП", url=checkout_url)],
            [InlineKeyboardButton(text="Помощь с оплатой", callback_data="support:cat:payment")],
            [InlineKeyboardButton(text="Главное меню", callback_data="menu:main")],
        ]
    )


def more_menu_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="Увеличить число устройств", callback_data="buy:upgrade")],
            [InlineKeyboardButton(text="Пригласить друга", callback_data="referral:show")],
            [InlineKeyboardButton(text="Помощь", callback_data="support:show")],
            [InlineKeyboardButton(text="← Назад", callback_data="menu:main")],
        ]
    )


def back_to_more_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="← Назад", callback_data="menu:more")],
            [InlineKeyboardButton(text="🏠 Главное меню", callback_data="menu:main")],
        ]
    )


def service_unavailable_keyboard(
    retry_callback: str = "buy:menu",
) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="Проверить ещё раз", callback_data=retry_callback)],
            [InlineKeyboardButton(text="Помощь", callback_data="support:show")],
            [InlineKeyboardButton(text="🏠 Главное меню", callback_data="menu:main")],
        ]
    )


def tariffs_keyboard(tariffs: list[Tariff]) -> InlineKeyboardMarkup:
    rows = []

    for tariff in tariffs:
        price = format_price(tariff.price_minor_units, tariff.currency)
        rows.append(
            [
                InlineKeyboardButton(
                    text=f"{tariff.title} — {price}",
                    callback_data=f"buy:tariff:{tariff.code}",
                ),
            ]
        )

    rows.append([InlineKeyboardButton(text="← Назад", callback_data="menu:main")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def purchase_devices_keyboard(device_options: tuple[int, ...]) -> InlineKeyboardMarkup:
    primary_values = tuple(value for value in (1, 2, 6, 12) if value in device_options)
    if not primary_values:
        primary_values = device_options[:4]
    other_values = tuple(value for value in device_options if value not in primary_values)

    rows = _device_option_rows(primary_values, callback_prefix="buy:devices")
    if other_values:
        rows.append(
            [
                InlineKeyboardButton(
                    text="Другое количество",
                    callback_data="buy:devices_other",
                )
            ]
        )
    rows.append([InlineKeyboardButton(text="← Назад", callback_data="menu:main")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def purchase_packages_keyboard(catalog: ProductCatalog) -> InlineKeyboardMarkup:
    primary_values = _featured_purchase_device_values(catalog.device_options)
    other_values = tuple(value for value in catalog.device_options if value not in primary_values)
    rows = _purchase_package_rows(catalog, primary_values)
    if other_values:
        rows.append(
            [
                InlineKeyboardButton(
                    text="Другие варианты",
                    callback_data="buy:packages_other",
                )
            ]
        )
    rows.append([InlineKeyboardButton(text="← Назад", callback_data="menu:main")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def purchase_other_packages_keyboard(catalog: ProductCatalog) -> InlineKeyboardMarkup:
    primary_values = set(_featured_purchase_device_values(catalog.device_options))
    other_values = tuple(value for value in catalog.device_options if value not in primary_values)
    rows = _purchase_package_rows(catalog, other_values)
    rows.append([InlineKeyboardButton(text="← Назад", callback_data="buy:menu")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def _featured_purchase_device_values(device_options: tuple[int, ...]) -> tuple[int, ...]:
    primary_values = tuple(value for value in (1, 2, 6, 12) if value in device_options)
    return primary_values or device_options[:4]


def _purchase_package_rows(
    catalog: ProductCatalog,
    device_values: tuple[int, ...],
) -> list[list[InlineKeyboardButton]]:
    buttons: list[InlineKeyboardButton] = []
    for devices in device_values:
        for period in catalog.period_options:
            amount = catalog.calculate_list_price(period, devices)
            buttons.append(
                InlineKeyboardButton(
                    text=(
                        f"{devices}×{period * 30} дн. · {format_price(amount, catalog.currency)}"
                    ),
                    callback_data=f"buy:period:{devices}:{period}",
                )
            )
    return [buttons[index : index + 2] for index in range(0, len(buttons), 2)]


def purchase_other_devices_keyboard(device_options: tuple[int, ...]) -> InlineKeyboardMarkup:
    primary_values = {value for value in (1, 2, 6, 12) if value in device_options}
    other_values = [value for value in device_options if value not in primary_values]
    rows = _device_option_rows(other_values, callback_prefix="buy:devices")
    rows.append([InlineKeyboardButton(text="← Назад", callback_data="buy:menu")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def _device_option_rows(
    values: tuple[int, ...] | list[int],
    *,
    callback_prefix: str,
) -> list[list[InlineKeyboardButton]]:
    buttons = [
        InlineKeyboardButton(
            text=_device_count_label(value),
            callback_data=f"{callback_prefix}:{value}",
        )
        for value in values
    ]
    return [buttons[index : index + 2] for index in range(0, len(buttons), 2)]


def _device_count_label(value: int) -> str:
    remainder_100 = value % 100
    remainder_10 = value % 10
    if 11 <= remainder_100 <= 14:
        suffix = "устройств"
    elif remainder_10 == 1:
        suffix = "устройство"
    elif 2 <= remainder_10 <= 4:
        suffix = "устройства"
    else:
        suffix = "устройств"
    return f"{value} {suffix}"


def _device_count_after_to_label(value: int) -> str:
    remainder_100 = value % 100
    remainder_10 = value % 10
    suffix = "устройства" if remainder_10 == 1 and remainder_100 != 11 else "устройств"
    return f"{value} {suffix}"


def upgrade_devices_keyboard(
    current_max_devices: int,
    device_options: tuple[int, ...],
) -> InlineKeyboardMarkup:
    values = [value for value in device_options if value > current_max_devices]
    primary_values = [value for value in (2, 6, 12) if value in values]
    other_values = [value for value in values if value not in primary_values]
    rows = _upgrade_device_option_rows(primary_values)
    if other_values:
        rows.append(
            [
                InlineKeyboardButton(
                    text="Другое количество",
                    callback_data="buy:upgrade_other",
                )
            ]
        )
    if not rows:
        rows.append(
            [InlineKeyboardButton(text="Максимальный лимит уже выбран", callback_data="menu:main")]
        )
    rows.append([InlineKeyboardButton(text="← Назад", callback_data="menu:main")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def upgrade_other_devices_keyboard(
    current_max_devices: int,
    device_options: tuple[int, ...],
) -> InlineKeyboardMarkup:
    primary_values = {value for value in (2, 6, 12) if value in device_options}
    values = [
        value
        for value in device_options
        if value > current_max_devices and value not in primary_values
    ]
    rows = _upgrade_device_option_rows(values)
    rows.append([InlineKeyboardButton(text="← Назад", callback_data="buy:upgrade")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def _upgrade_device_option_rows(values: list[int]) -> list[list[InlineKeyboardButton]]:
    buttons = [
        InlineKeyboardButton(
            text=f"До {_device_count_after_to_label(value)}",
            callback_data=f"buy:upgrade_devices:{value}",
        )
        for value in values
    ]
    return [buttons[index : index + 2] for index in range(0, len(buttons), 2)]


def purchase_periods_keyboard(
    catalog: ProductCatalog,
    *,
    selected_devices: int | None,
    price_devices: int,
    operation_kind: str,
    grandfathered_device_limit: int | None = None,
) -> InlineKeyboardMarkup:
    rows: list[list[InlineKeyboardButton]] = []
    best_period = max(catalog.period_options)
    for period in catalog.period_options:
        callback_data = (
            f"buy:period:{selected_devices}:{period}"
            if selected_devices is not None
            else f"buy:{operation_kind}:period:{period}"
        )
        period_label = days_ru(period * 30)
        amount = catalog.calculate_list_price(
            period,
            price_devices,
            grandfathered_value=grandfathered_device_limit,
        )
        discount = catalog.duration_discounts.get(period, 0)
        if period == best_period and discount > 0:
            benefit = f" · выгоднее всего · −{discount}%"
        elif discount > 0:
            benefit = f" · экономия {discount}%"
        else:
            benefit = ""
        label = f"{period_label} — {format_price(amount, catalog.currency)}{benefit}"
        rows.append([InlineKeyboardButton(text=label, callback_data=callback_data)])

    back_callback = "buy:menu" if selected_devices is not None else "menu:main"
    rows.append([InlineKeyboardButton(text="← Назад", callback_data=back_callback)])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def quote_confirmation_keyboard(
    public_quote_id: str,
    *,
    edit_callback: str = "buy:menu",
    complimentary: bool = False,
    sbp_url: str | None = None,
    stars_amount: int | None = None,
    sbp_amount_minor_units: int | None = None,
) -> InlineKeyboardMarkup:
    if complimentary:
        rows = [
            [
                InlineKeyboardButton(
                    text="Получить бесплатно", callback_data=f"buy:pay:{public_quote_id}"
                )
            ]
        ]
    else:
        stars_label = (
            f"Оплатить звёздами — {stars_amount} ⭐"
            if stars_amount is not None
            else "Оплатить звёздами"
        )
        rows = [
            [InlineKeyboardButton(text=stars_label, callback_data=f"buy:pay:{public_quote_id}")]
        ]
        if sbp_url is not None:
            sbp_label = (
                f"Оплатить по СБП — {format_price(sbp_amount_minor_units, 'RUB')}"
                if sbp_amount_minor_units is not None
                else "Оплатить по СБП"
            )
            rows.append([InlineKeyboardButton(text=sbp_label, url=sbp_url)])
    rows.extend(
        [
            [InlineKeyboardButton(text="Изменить", callback_data=edit_callback)],
            [InlineKeyboardButton(text="Отмена", callback_data="menu:main")],
        ]
    )
    return InlineKeyboardMarkup(
        inline_keyboard=rows,
    )


def subscription_keyboard(has_subscription: bool) -> InlineKeyboardMarkup:
    rows: list[list[InlineKeyboardButton]] = []
    if has_subscription:
        rows.extend(
            [
                [InlineKeyboardButton(text="Открыть в Happ", callback_data="credential:create")],
                [InlineKeyboardButton(text="Мои устройства", callback_data="subscription:devices")],
                [InlineKeyboardButton(text="Продлить доступ", callback_data="buy:renew")],
            ]
        )
    else:
        rows.append([InlineKeyboardButton(text="Купить доступ", callback_data="buy:menu")])
    rows.append([InlineKeyboardButton(text="🏠 Главное меню", callback_data="menu:main")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def device_limit_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="Мои устройства",
                    callback_data="subscription:devices",
                )
            ],
            [InlineKeyboardButton(text="🏠 Главное меню", callback_data="menu:main")],
        ],
    )


def reset_confirm_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="✅ Да, сбросить", callback_data="subscription:reset_do")],
            [InlineKeyboardButton(text="Отмена", callback_data="subscription:show")],
        ],
    )


def after_purchase_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="Открыть в Happ",
                    callback_data="credential:create",
                )
            ],
            [InlineKeyboardButton(text="Установить Happ", callback_data="onboarding:install")],
            [InlineKeyboardButton(text="🏠 Главное меню", callback_data="menu:main")],
        ],
    )


def platform_selection_keyboard(preferred_platform: str | None = None) -> InlineKeyboardMarkup:
    if preferred_platform is not None:
        try:
            platform = Platform(preferred_platform)
        except ValueError:
            platform = None
        if platform is not None:
            return InlineKeyboardMarkup(
                inline_keyboard=[
                    [
                        InlineKeyboardButton(
                            text=f"Установить Happ для: {platform_label_ru(platform)}",
                            callback_data=f"onboarding:platform:{platform.value}",
                        )
                    ],
                    [
                        InlineKeyboardButton(
                            text="Выбрать другую платформу",
                            callback_data="onboarding:install",
                        )
                    ],
                    [InlineKeyboardButton(text="🏠 Главное меню", callback_data="menu:main")],
                ]
            )

    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="📱 Android", callback_data="onboarding:platform:android")],
            [
                InlineKeyboardButton(
                    text="🍎 iPhone / iPad", callback_data="onboarding:platform:ios"
                )
            ],
            [InlineKeyboardButton(text="🪟 Windows", callback_data="onboarding:platform:windows")],
            [InlineKeyboardButton(text="🍏 Mac", callback_data="onboarding:platform:macos")],
            [InlineKeyboardButton(text="📺 Телевизор", callback_data="onboarding:tv")],
            [InlineKeyboardButton(text="Другие устройства", callback_data="onboarding:other")],
            [InlineKeyboardButton(text="🏠 Главное меню", callback_data="menu:main")],
        ]
    )


def other_platforms_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="🐧 Linux", callback_data="onboarding:platform:linux")],
            [InlineKeyboardButton(text="📡 Роутер", callback_data="onboarding:router")],
            [InlineKeyboardButton(text="Не вижу своего устройства", callback_data="support:show")],
            [InlineKeyboardButton(text="← Назад", callback_data="onboarding:install")],
        ]
    )


def tv_platform_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="Android TV / Google TV",
                    callback_data="onboarding:platform:android_tv",
                )
            ],
            [
                InlineKeyboardButton(
                    text="Apple TV",
                    callback_data="onboarding:platform:apple_tv",
                )
            ],
            [InlineKeyboardButton(text="Другой телевизор", callback_data="onboarding:other")],
            [InlineKeyboardButton(text="← Назад", callback_data="onboarding:install")],
        ]
    )


def onboarding_install_keyboard(platform: ClientAppPlatform) -> InlineKeyboardMarkup:
    rows = [
        [
            InlineKeyboardButton(
                text=platform.primary_link().label,
                url=platform.primary_link().url,
            )
        ],
        [InlineKeyboardButton(text="Открыть в Happ", callback_data="credential:create")],
    ]

    if len(platform.install_links) > 1:
        rows.append(
            [
                InlineKeyboardButton(
                    text="Другой способ установки",
                    callback_data=f"onboarding:alt:{platform.platform.value}",
                )
            ]
        )

    rows.extend(
        [
            [InlineKeyboardButton(text="Не получилось", callback_data="support:show")],
            [InlineKeyboardButton(text="← Назад", callback_data="onboarding:install")],
            [InlineKeyboardButton(text="🏠 Главное меню", callback_data="menu:main")],
        ]
    )
    return InlineKeyboardMarkup(inline_keyboard=rows)


def onboarding_access_keyboard(product_name: str) -> InlineKeyboardMarkup:
    _ = product_name
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="Открыть в Happ",
                    callback_data="credential:create",
                )
            ],
            [InlineKeyboardButton(text="Установить Happ", callback_data="onboarding:install")],
            [InlineKeyboardButton(text="Помощь", callback_data="support:show")],
            [InlineKeyboardButton(text="🏠 Главное меню", callback_data="menu:main")],
        ]
    )


def credential_failure_keyboard(
    *,
    limit_reached: bool = False,
    recovery_required: bool = False,
) -> InlineKeyboardMarkup:
    rows = [[InlineKeyboardButton(text="Попробовать снова", callback_data="credential:create")]]
    if limit_reached or recovery_required:
        rows.append(
            [InlineKeyboardButton(text="Мои устройства", callback_data="subscription:devices")]
        )
    if limit_reached:
        rows.append([InlineKeyboardButton(text="Увеличить лимит", callback_data="buy:upgrade")])
    rows.extend(
        [
            [InlineKeyboardButton(text="Помощь", callback_data="support:show")],
            [InlineKeyboardButton(text="🏠 Главное меню", callback_data="menu:main")],
        ]
    )
    return InlineKeyboardMarkup(inline_keyboard=rows)


def first_fetch_check_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="Проверить, появилась ли подписка",
                    callback_data="credential:check",
                )
            ],
            [InlineKeyboardButton(text="Помощь", callback_data="support:show")],
            [InlineKeyboardButton(text="🏠 Главное меню", callback_data="menu:main")],
        ]
    )


def platform_label_ru(platform: Platform) -> str:
    return {
        Platform.ANDROID: "Android-устройство",
        Platform.IOS: "iPhone / iPad",
        Platform.WINDOWS: "Windows-компьютер",
        Platform.MACOS: "Mac",
        Platform.LINUX: "Linux-компьютер",
        Platform.ANDROID_TV: "Android TV",
        Platform.APPLE_TV: "Apple TV",
        Platform.ROUTER: "устройство",
        Platform.UNSUPPORTED: "устройство",
    }[platform]


def devices_keyboard(device_public_ids: list[tuple[str, str]]) -> InlineKeyboardMarkup:
    rows: list[list[InlineKeyboardButton]] = []
    for device_public_id, label in device_public_ids:
        rows.append(
            [
                InlineKeyboardButton(
                    text=f"Управлять: {label}",
                    callback_data=f"device:manage:{device_public_id}",
                )
            ]
        )
    rows.append(
        [
            InlineKeyboardButton(
                text="Открыть в Happ",
                callback_data="credential:create",
            )
        ]
    )
    rows.append([InlineKeyboardButton(text="← Назад", callback_data="subscription:show")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def device_bulk_management_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="Запретить обновление для всех",
                    callback_data="subscription:reset_confirm",
                )
            ],
            [InlineKeyboardButton(text="← Назад", callback_data="subscription:devices")],
        ]
    )


def device_management_keyboard(
    device_public_id: str,
    *,
    can_transfer: bool = False,
    disabled: bool = False,
) -> InlineKeyboardMarkup:
    _ = can_transfer
    action = (
        InlineKeyboardButton(
            text="Подключить снова",
            callback_data=f"device:enable:{device_public_id}",
        )
        if disabled
        else InlineKeyboardButton(
            text="Запретить обновление",
            callback_data=f"device:revoke:{device_public_id}",
        )
    )
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [action],
            [InlineKeyboardButton(text="← Назад", callback_data="subscription:devices")],
        ]
    )


def support_categories_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="VPN не подключается",
                    callback_data="support:cat:vpn",
                )
            ],
            [
                InlineKeyboardButton(
                    text="Не получается добавить в Happ",
                    callback_data="support:cat:happ",
                )
            ],
            [InlineKeyboardButton(text="Закончились места", callback_data="support:cat:slots")],
            [InlineKeyboardButton(text="Проблема с оплатой", callback_data="support:cat:payment")],
            [InlineKeyboardButton(text="Другая проблема", callback_data="support:cat:other")],
            [InlineKeyboardButton(text="🏠 Главное меню", callback_data="menu:main")],
        ],
    )


def format_price(amount_minor_units: int, currency: str) -> str:
    if currency.upper() == "XTR":
        return f"{amount_minor_units} ⭐"

    if currency.upper() == "RUB":
        rubles, kopecks = divmod(amount_minor_units, 100)
        return f"{rubles} ₽" if kopecks == 0 else f"{rubles},{kopecks:02d} ₽"

    major = amount_minor_units / 100
    return f"{major:.0f} {currency}"


def credential_delivery_keyboard(
    connection_url: str,
    *,
    happ_deep_link: str | None,
    can_copy: bool,
) -> InlineKeyboardMarkup:
    rows: list[list[InlineKeyboardButton]] = []
    if happ_deep_link is not None:
        rows.append([InlineKeyboardButton(text="Открыть в Happ", url=happ_deep_link)])
    if can_copy:
        rows.append(
            [
                InlineKeyboardButton(
                    text="Скопировать ссылку",
                    copy_text=CopyTextButton(text=connection_url),
                )
            ]
        )
    rows.extend(
        [
            [InlineKeyboardButton(text="Не появилось в Happ?", callback_data="credential:check")],
            [InlineKeyboardButton(text="🏠 Главное меню", callback_data="menu:main")],
        ]
    )
    return InlineKeyboardMarkup(inline_keyboard=rows)


def device_action_confirm_keyboard(device_public_id: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="Отключить",
                    callback_data=f"device:revoke_do:{device_public_id}",
                )
            ],
            [InlineKeyboardButton(text="Отмена", callback_data="subscription:devices")],
        ]
    )
