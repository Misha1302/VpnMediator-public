from __future__ import annotations

from datetime import UTC, datetime
from html import escape
from zoneinfo import ZoneInfo

from vpn_access_bot.expiration import access_through_date

MOSCOW_TIMEZONE = ZoneInfo("Europe/Moscow")
MONTHS_GENITIVE = {
    1: "января",
    2: "февраля",
    3: "марта",
    4: "апреля",
    5: "мая",
    6: "июня",
    7: "июля",
    8: "августа",
    9: "сентября",
    10: "октября",
    11: "ноября",
    12: "декабря",
}


def format_datetime_ru(value: datetime, *, include_year: bool = False) -> str:
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)

    local_value = value.astimezone(MOSCOW_TIMEZONE)
    year = f" {local_value.year} года" if include_year else ""
    return (
        f"{local_value.day} {MONTHS_GENITIVE[local_value.month]}{year}, "
        f"{local_value.hour:02d}:{local_value.minute:02d} МСК"
    )


def format_iso_datetime_ru(value: str | None, *, fallback: str = "скоро") -> str:
    if not value:
        return fallback
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return fallback
    return format_datetime_ru(parsed)


def plural_ru(value: int, one: str, few: str, many: str) -> str:
    absolute = abs(value)
    last_two = absolute % 100
    last = absolute % 10

    if 11 <= last_two <= 14:
        form = many
    elif last == 1:
        form = one
    elif 2 <= last <= 4:
        form = few
    else:
        form = many

    return f"{value} {form}"


def devices_ru(value: int) -> str:
    return plural_ru(value, "устройство", "устройства", "устройств")


def months_ru(value: int) -> str:
    return plural_ru(value, "месяц", "месяца", "месяцев")


def days_ru(value: int) -> str:
    return plural_ru(value, "день", "дня", "дней")


def escape_html(value: object) -> str:
    return escape(str(value), quote=True)


def format_access_through_date_ru(
    value: datetime,
    business_timezone: str = "Europe/Moscow",
) -> str:
    through = access_through_date(value, business_timezone)
    return f"{through.day} {MONTHS_GENITIVE[through.month]} {through.year} года включительно"


def format_local_date_ru(
    value: datetime,
    business_timezone: str = "Europe/Moscow",
) -> str:
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    local = value.astimezone(ZoneInfo(business_timezone))
    return f"{local.day} {MONTHS_GENITIVE[local.month]} {local.year} года"
