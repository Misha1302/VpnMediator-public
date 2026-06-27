from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass

from vpn_access_bot.config import Settings
from vpn_access_bot.constants import (
    PAYMENT_MODE_TELEGRAM_STARS,
    PAYMENT_MODE_YOOKASSA_SBP,
    RUSSIAN_RUBLE_CURRENCY,
    TELEGRAM_STARS_CURRENCY,
)


@dataclass(frozen=True)
class ProductCatalog:
    period_options: tuple[int, ...]
    device_options: tuple[int, ...]
    base_device_month_stars: int
    base_device_month_rub_kopecks: int
    duration_discounts: dict[int, int]
    currency: str
    pricing_identity: str
    rub_pricing_identity: str

    @classmethod
    def from_settings(cls, settings: Settings) -> ProductCatalog:
        period_options = _parse_positive_options(
            settings.purchasable_period_options,
            "PURCHASABLE_PERIOD_OPTIONS",
        )
        device_options = _parse_positive_options(
            settings.purchasable_device_options,
            "PURCHASABLE_DEVICE_OPTIONS",
        )
        discounts = _parse_discounts(settings.pricing_duration_discounts)

        unknown_discount_periods = set(discounts) - set(period_options)
        if unknown_discount_periods:
            values = ", ".join(str(value) for value in sorted(unknown_discount_periods))
            raise ValueError("PRICING_DURATION_DISCOUNTS contains unsupported periods: " + values)

        if settings.pricing_base_device_month_stars <= 0:
            raise ValueError("PRICING_BASE_DEVICE_MONTH_STARS must be positive.")
        if settings.pricing_base_device_month_rub_kopecks <= 0:
            raise ValueError("PRICING_BASE_DEVICE_MONTH_RUB_KOPECKS must be positive.")

        payload = {
            "version": settings.pricing_version,
            "periods": period_options,
            "devices": device_options,
            "base": settings.pricing_base_device_month_stars,
            "discounts": sorted(discounts.items()),
            "currency": TELEGRAM_STARS_CURRENCY,
        }
        digest = hashlib.sha256(
            json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
        ).hexdigest()[:12]
        rub_payload = {
            **payload,
            "base": settings.pricing_base_device_month_rub_kopecks,
            "currency": RUSSIAN_RUBLE_CURRENCY,
        }
        rub_digest = hashlib.sha256(
            json.dumps(rub_payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
        ).hexdigest()[:12]

        catalog = cls(
            period_options=period_options,
            device_options=device_options,
            base_device_month_stars=settings.pricing_base_device_month_stars,
            base_device_month_rub_kopecks=settings.pricing_base_device_month_rub_kopecks,
            duration_discounts=discounts,
            currency=TELEGRAM_STARS_CURRENCY,
            pricing_identity=f"{settings.pricing_version}:{digest}",
            rub_pricing_identity=f"{settings.pricing_version}:rub:{rub_digest}",
        )
        catalog.validate_prices()
        return catalog

    def validate_period(self, period_count: int, *, allow_zero: bool = False) -> None:
        if allow_zero and period_count == 0:
            return
        if period_count not in self.period_options:
            raise ValueError("unsupported_period_option")

    def validate_device_limit(
        self,
        max_devices: int,
        *,
        grandfathered_value: int | None = None,
    ) -> None:
        if grandfathered_value is not None and max_devices == grandfathered_value:
            return
        if max_devices not in self.device_options:
            raise ValueError("unsupported_device_option")

    def larger_device_options(self, current_max_devices: int) -> tuple[int, ...]:
        return tuple(value for value in self.device_options if value > current_max_devices)

    def calculate_list_price(
        self,
        period_count: int,
        max_devices: int,
        *,
        grandfathered_value: int | None = None,
    ) -> int:
        self.validate_period(period_count)
        self.validate_device_limit(
            max_devices,
            grandfathered_value=grandfathered_value,
        )
        gross = period_count * max_devices * self.base_device_month_stars
        discount_percent = self.duration_discounts.get(period_count, 0)
        return gross - gross * discount_percent // 100

    def base_price_for_provider(self, provider: str) -> int:
        if provider == PAYMENT_MODE_TELEGRAM_STARS:
            return self.base_device_month_stars
        if provider == PAYMENT_MODE_YOOKASSA_SBP:
            return self.base_device_month_rub_kopecks
        raise ValueError("unsupported_payment_provider")

    def currency_for_provider(self, provider: str) -> str:
        if provider == PAYMENT_MODE_TELEGRAM_STARS:
            return TELEGRAM_STARS_CURRENCY
        if provider == PAYMENT_MODE_YOOKASSA_SBP:
            return RUSSIAN_RUBLE_CURRENCY
        raise ValueError("unsupported_payment_provider")

    def pricing_identity_for_provider(self, provider: str) -> str:
        if provider == PAYMENT_MODE_TELEGRAM_STARS:
            return self.pricing_identity
        if provider == PAYMENT_MODE_YOOKASSA_SBP:
            return self.rub_pricing_identity
        raise ValueError("unsupported_payment_provider")

    def calculate_list_price_for_provider(
        self,
        provider: str,
        period_count: int,
        max_devices: int,
        *,
        grandfathered_value: int | None = None,
    ) -> int:
        self.validate_period(period_count)
        self.validate_device_limit(max_devices, grandfathered_value=grandfathered_value)
        gross = period_count * max_devices * self.base_price_for_provider(provider)
        discount_percent = self.duration_discounts.get(period_count, 0)
        return gross - gross * discount_percent // 100

    def validate_prices(self) -> None:
        for period_count in self.period_options:
            for max_devices in self.device_options:
                gross = period_count * max_devices * self.base_device_month_stars
                discount = gross * self.duration_discounts.get(period_count, 0) // 100
                if gross - discount <= 0:
                    raise ValueError("Product catalog contains a non-positive paid price.")
                rub_gross = period_count * max_devices * self.base_device_month_rub_kopecks
                rub_discount = rub_gross * self.duration_discounts.get(period_count, 0) // 100
                if rub_gross - rub_discount <= 0:
                    raise ValueError("RUB product catalog contains a non-positive paid price.")


def _parse_positive_options(value: str, setting_name: str) -> tuple[int, ...]:
    try:
        options = tuple(int(part.strip()) for part in value.split(",") if part.strip())
    except ValueError as exception:
        raise ValueError(f"{setting_name} must be a comma-separated integer list.") from exception

    if not options:
        raise ValueError(f"{setting_name} must not be empty.")
    if any(option <= 0 for option in options):
        raise ValueError(f"{setting_name} values must be positive.")
    if len(set(options)) != len(options):
        raise ValueError(f"{setting_name} values must be unique.")
    if options != tuple(sorted(options)):
        raise ValueError(f"{setting_name} values must be sorted.")
    return options


def _parse_discounts(value: str) -> dict[int, int]:
    discounts: dict[int, int] = {}
    for item in value.split(","):
        if not item.strip():
            continue
        try:
            period_text, discount_text = item.split(":", maxsplit=1)
            period = int(period_text.strip())
            discount = int(discount_text.strip())
        except (ValueError, TypeError) as exception:
            raise ValueError(
                "PRICING_DURATION_DISCOUNTS must contain PERIOD:PERCENT pairs."
            ) from exception
        if period <= 0 or not 0 <= discount < 100:
            raise ValueError("Pricing discount periods must be positive and percent must be 0..99.")
        if period in discounts:
            raise ValueError("Pricing discount periods must be unique.")
        discounts[period] = discount
    return discounts
