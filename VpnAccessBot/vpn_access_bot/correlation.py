from __future__ import annotations

import logging
from contextvars import ContextVar, Token
from uuid import uuid4

from aiogram import BaseMiddleware
from aiogram.types import TelegramObject

from vpn_access_bot.telegram.context import get_bot_key

_correlation_id: ContextVar[str | None] = ContextVar("correlation_id", default=None)


def get_correlation_id() -> str | None:
    return _correlation_id.get()


def set_correlation_id(value: str) -> Token[str | None]:
    return _correlation_id.set(value)


def reset_correlation_id(token: Token[str | None]) -> None:
    _correlation_id.reset(token)


class CorrelationIdMiddleware(BaseMiddleware):
    async def __call__(self, handler, event: TelegramObject, data: dict):
        update_id = getattr(event, "update_id", None)
        bot_key = get_bot_key() or "unknown"
        prefix = f"tg-{bot_key}-{update_id}" if update_id is not None else f"tg-{bot_key}"
        correlation_id = f"{prefix}-{uuid4().hex[:16]}"
        token = set_correlation_id(correlation_id)
        data["correlation_id"] = correlation_id

        try:
            return await handler(event, data)
        finally:
            reset_correlation_id(token)


class CorrelationIdLogFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        record.correlation_id = get_correlation_id() or "-"
        return True
