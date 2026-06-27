from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError

from vpn_access_bot.telegram.bot_registry import BotRegistry


class NotificationRecipientUnavailable(RuntimeError):
    pass


@dataclass(frozen=True)
class NotificationSendResult:
    message: Any
    delivery_bot_key: str


class NotificationSender:
    def __init__(
        self,
        registry: BotRegistry,
        session_factory: Callable[[], Any] | None = None,
        default_bot_key: str | None = None,
    ) -> None:
        self._registry = registry
        self._session_factory = session_factory
        self._default_bot_key = default_bot_key

    async def send_message(
        self,
        telegram_id: int,
        text: str,
        *,
        bot_key: str | None = None,
        allow_fallback: bool = True,
        **kwargs,
    ) -> NotificationSendResult:
        candidates = await self._delivery_candidates(telegram_id, bot_key)
        if not allow_fallback and candidates:
            candidates = candidates[:1]
        last_exception: Exception | None = None
        for runtime in candidates:
            if runtime.bot is None:
                continue
            try:
                result = await runtime.bot.send_message(telegram_id, text, **kwargs)
                await self._mark_channel_available(telegram_id, runtime.key)
                return NotificationSendResult(result, runtime.key)
            except TelegramForbiddenError as exception:
                last_exception = exception
                await self._mark_channel_blocked(telegram_id, runtime.key)
                continue
            except TelegramBadRequest as exception:
                if "chat not found" not in exception.message.casefold():
                    raise
                last_exception = exception
                await self._mark_channel_blocked(telegram_id, runtime.key)
                continue
        if last_exception is not None:
            raise last_exception
        raise NotificationRecipientUnavailable(
            "No verified Telegram bot is available for message delivery."
        )

    async def _delivery_candidates(self, telegram_id: int, bot_key: str | None):
        if bot_key is not None:
            return self._registry.delivery_candidates(bot_key)

        preferred: list[str] = []
        blocked: set[str] = set()
        if self._session_factory is not None:
            from vpn_access_bot.repositories import TelegramChannelRepository

            async with self._session_factory() as session:
                preferred, blocked = await TelegramChannelRepository(
                    session
                ).delivery_preferences_for_telegram_id(telegram_id)

        if self._default_bot_key is not None and self._default_bot_key not in preferred:
            preferred.append(self._default_bot_key)
        return self._registry.delivery_candidates_for_keys(preferred, excluded_bot_keys=blocked)

    async def _mark_channel_blocked(self, telegram_id: int, bot_key: str) -> None:
        if self._session_factory is None:
            return
        from vpn_access_bot.repositories import TelegramChannelRepository, UserRepository

        async with self._session_factory() as session:
            user = await UserRepository(session).get_by_telegram_id(telegram_id)
            if user is not None:
                await TelegramChannelRepository(session).mark_user_channel_blocked(
                    user.id,
                    bot_key,
                )

    async def _mark_channel_available(self, telegram_id: int, bot_key: str) -> None:
        if self._session_factory is None:
            return
        from vpn_access_bot.repositories import TelegramChannelRepository, UserRepository

        async with self._session_factory() as session:
            user = await UserRepository(session).get_by_telegram_id(telegram_id)
            if user is not None:
                await TelegramChannelRepository(session).touch_user_channel(user.id, bot_key)
