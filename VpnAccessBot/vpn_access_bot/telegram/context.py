from __future__ import annotations

from contextvars import ContextVar, Token

_bot_key: ContextVar[str | None] = ContextVar("bot_key", default=None)
_telegram_bot_id: ContextVar[int | None] = ContextVar("telegram_bot_id", default=None)


def get_bot_key() -> str | None:
    return _bot_key.get()


def get_telegram_bot_id() -> int | None:
    return _telegram_bot_id.get()


def set_bot_context(
    bot_key: str, telegram_bot_id: int
) -> tuple[Token[str | None], Token[int | None]]:
    return _bot_key.set(bot_key), _telegram_bot_id.set(telegram_bot_id)


def reset_bot_context(tokens: tuple[Token[str | None], Token[int | None]]) -> None:
    bot_key_token, bot_id_token = tokens
    _telegram_bot_id.reset(bot_id_token)
    _bot_key.reset(bot_key_token)
