from __future__ import annotations

from aiogram import BaseMiddleware, Bot
from aiogram.types import TelegramObject

from vpn_access_bot.telegram.bot_registry import BotRegistry
from vpn_access_bot.telegram.context import reset_bot_context, set_bot_context


class BotIdentityMiddleware(BaseMiddleware):
    def __init__(self, registry: BotRegistry) -> None:
        self._registry = registry

    async def __call__(self, handler, event: TelegramObject, data: dict):
        bot = data.get("bot")
        if not isinstance(bot, Bot):
            raise RuntimeError("Telegram update does not contain a bot instance.")
        runtime = self._registry.resolve(bot)
        if runtime.telegram_bot_id is None:
            raise RuntimeError("Telegram bot identity has not been verified.")
        tokens = set_bot_context(runtime.key, runtime.telegram_bot_id)
        data["bot_key"] = runtime.key
        data["telegram_bot_id"] = runtime.telegram_bot_id
        data["verified_bot_username"] = runtime.username
        try:
            return await handler(event, data)
        finally:
            reset_bot_context(tokens)
