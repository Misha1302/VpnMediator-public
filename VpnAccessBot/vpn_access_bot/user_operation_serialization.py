from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass

from aiogram import BaseMiddleware
from aiogram.types import TelegramObject, Update


@dataclass(slots=True)
class _LockEntry:
    lock: asyncio.Lock
    users: int = 0


class UserOperationSerializationMiddleware(BaseMiddleware):
    """Serialize ordinary state-changing updates for one bot/user pair.

    Telegram pre-checkout queries keep their dedicated deadline-sensitive path. Successful
    payments are persisted by the durable payment inbox and are not delayed by presentation
    callbacks from the same user.
    """

    def __init__(self) -> None:
        self._entries: dict[tuple[str, int], _LockEntry] = {}
        self._entries_guard = asyncio.Lock()

    async def __call__(
        self,
        handler: Callable[[TelegramObject, dict], Awaitable[object]],
        event: TelegramObject,
        data: dict,
    ) -> object | None:
        if not isinstance(event, Update) or self._should_bypass(event):
            return await handler(event, data)

        actor_id = self._actor_id(event)
        if actor_id is None:
            return await handler(event, data)

        bot_key = str(data.get("bot_key") or "default")
        key = (bot_key, actor_id)
        entry = await self._acquire_entry(key)
        try:
            async with entry.lock:
                return await handler(event, data)
        finally:
            await self._release_entry(key, entry)

    @staticmethod
    def _should_bypass(update: Update) -> bool:
        if update.pre_checkout_query is not None:
            return True
        message = update.message or update.edited_message
        return message is not None and message.successful_payment is not None

    @staticmethod
    def _actor_id(update: Update) -> int | None:
        if update.callback_query is not None:
            return update.callback_query.from_user.id
        message = update.message or update.edited_message
        if message is not None and message.from_user is not None:
            return message.from_user.id
        return None

    async def _acquire_entry(self, key: tuple[str, int]) -> _LockEntry:
        async with self._entries_guard:
            entry = self._entries.get(key)
            if entry is None:
                entry = _LockEntry(lock=asyncio.Lock())
                self._entries[key] = entry
            entry.users += 1
            return entry

    async def _release_entry(self, key: tuple[str, int], entry: _LockEntry) -> None:
        async with self._entries_guard:
            entry.users -= 1
            if entry.users == 0 and not entry.lock.locked():
                self._entries.pop(key, None)
