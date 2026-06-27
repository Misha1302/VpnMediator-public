from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from aiogram import Bot
from aiogram.client.default import DefaultBotProperties
from aiogram.client.session.aiohttp import AiohttpSession
from aiogram.enums import ParseMode

from vpn_access_bot.config import TelegramBotDefinition


@dataclass
class BotRuntime:
    definition: TelegramBotDefinition
    bot: Bot | None
    telegram_bot_id: int | None
    username: str | None
    status: str
    error_code: str | None = None
    last_verified_at_utc: datetime | None = None

    @property
    def key(self) -> str:
        return self.definition.key


class BotRegistry:
    def __init__(self, definitions: list[TelegramBotDefinition]) -> None:
        if not definitions:
            raise ValueError("At least one Telegram bot definition is required.")
        keys = [definition.key for definition in definitions]
        if len(keys) != len(set(keys)):
            raise ValueError("Telegram bot keys must be unique.")
        self._definitions = list(definitions)
        self._runtimes: dict[str, BotRuntime] = {}
        self._keys_by_object_id: dict[int, str] = {}

    async def initialize(self) -> None:
        for definition in self._definitions:
            runtime = await self._initialize_bot(definition)
            self._runtimes[definition.key] = runtime
            if runtime.bot is not None:
                self._keys_by_object_id[id(runtime.bot)] = definition.key
            if runtime.status != "polling" and definition.required:
                await self.close()
                raise RuntimeError(
                    f"Required Telegram bot '{definition.key}' failed startup verification: "
                    f"{runtime.error_code or 'unknown_error'}."
                )

        numeric_ids = [
            runtime.telegram_bot_id
            for runtime in self._runtimes.values()
            if runtime.telegram_bot_id is not None
        ]
        if len(numeric_ids) != len(set(numeric_ids)):
            await self.close()
            raise RuntimeError(
                "Configured Telegram bot tokens resolve to duplicate numeric bot IDs."
            )

    async def _initialize_bot(self, definition: TelegramBotDefinition) -> BotRuntime:
        bot: Bot | None = None
        try:
            session = AiohttpSession(proxy=definition.proxy_url) if definition.proxy_url else None
            bot = Bot(
                token=definition.token.get_secret_value(),
                session=session,
                default=DefaultBotProperties(parse_mode=ParseMode.HTML),
            )
            identity = await bot.get_me()
            username = identity.username or ""
            expected = definition.expected_username
            if expected and username.casefold() != expected.lstrip("@").casefold():
                await bot.session.close()
                return BotRuntime(
                    definition=definition,
                    bot=None,
                    telegram_bot_id=identity.id,
                    username=username,
                    status="invalid",
                    error_code="username_mismatch",
                    last_verified_at_utc=datetime.now(UTC),
                )
            return BotRuntime(
                definition=definition,
                bot=bot,
                telegram_bot_id=identity.id,
                username=username,
                status="polling",
                last_verified_at_utc=datetime.now(UTC),
            )
        except Exception as exception:
            if bot is not None:
                await bot.session.close()
            return BotRuntime(
                definition=definition,
                bot=None,
                telegram_bot_id=None,
                username=None,
                status="invalid",
                error_code=type(exception).__name__,
                last_verified_at_utc=datetime.now(UTC),
            )

    @property
    def bots(self) -> list[Bot]:
        return [runtime.bot for runtime in self._runtimes.values() if runtime.bot is not None]

    @property
    def runtimes(self) -> list[BotRuntime]:
        return list(self._runtimes.values())

    def resolve(self, bot: Bot) -> BotRuntime:
        key = self._keys_by_object_id.get(id(bot))
        if key is None:
            raise RuntimeError("Telegram bot instance is not registered.")
        return self._runtimes[key]

    def get(self, bot_key: str) -> BotRuntime | None:
        return self._runtimes.get(bot_key)

    def delivery_candidates(self, preferred_bot_key: str | None) -> list[BotRuntime]:
        preferred_keys = [preferred_bot_key] if preferred_bot_key is not None else []
        return self.delivery_candidates_for_keys(preferred_keys)

    def delivery_candidates_for_keys(
        self,
        preferred_bot_keys: list[str],
        *,
        excluded_bot_keys: set[str] | None = None,
    ) -> list[BotRuntime]:
        excluded = excluded_bot_keys or set()
        active = [
            runtime
            for runtime in self._runtimes.values()
            if runtime.bot is not None and runtime.key not in excluded
        ]
        ordered: list[BotRuntime] = []
        seen: set[str] = set()
        for key in preferred_bot_keys:
            runtime = self._runtimes.get(key)
            if (
                runtime is None
                or runtime.bot is None
                or runtime.key in excluded
                or runtime.key in seen
            ):
                continue
            ordered.append(runtime)
            seen.add(runtime.key)
        ordered.extend(runtime for runtime in active if runtime.key not in seen)
        return ordered

    def health_snapshot(self) -> list[dict[str, Any]]:
        return [
            {
                "key": runtime.key,
                "username": runtime.username,
                "telegramBotId": runtime.telegram_bot_id,
                "status": runtime.status,
                "required": runtime.definition.required,
                "errorCode": runtime.error_code,
                "lastVerifiedAtUtc": runtime.last_verified_at_utc,
            }
            for runtime in self._runtimes.values()
        ]

    @property
    def required_bots_ready(self) -> bool:
        return all(
            runtime.status == "polling"
            for runtime in self._runtimes.values()
            if runtime.definition.required
        )

    async def close(self) -> None:
        sessions: set[int] = set()
        for runtime in self._runtimes.values():
            if runtime.bot is None:
                continue
            session = runtime.bot.session
            if id(session) in sessions:
                continue
            sessions.add(id(session))
            await session.close()
