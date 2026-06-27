from __future__ import annotations

import io
import json
import logging
from datetime import UTC, datetime
from pathlib import Path

import pytest
from sqlalchemy import func, select

from vpn_access_bot.config import Settings
from vpn_access_bot.db import Database
from vpn_access_bot.logging_config import (
    BoundedQueueHandler,
    JsonLineFormatter,
    RedactingFormatter,
    redact_text,
)
from vpn_access_bot.models import User, UserBotChannel
from vpn_access_bot.repositories import TelegramChannelRepository, UserRepository
from vpn_access_bot.telegram.context import reset_bot_context, set_bot_context


def _development_settings(**overrides: object) -> Settings:
    values: dict[str, object] = {
        "TELEGRAM_BOT_TOKEN": "123456789:" + "A" * 40,
        "MEDIATOR_ADMIN_TOKEN": "m" * 40,
        "APP_ENV": "development",
    }
    values.update(overrides)
    return Settings(**values)


def test_indexed_multibot_json_configuration_preserves_secret_values() -> None:
    payload = json.dumps(
        [
            {
                "key": "razakov",
                "token": "123456789:" + "A" * 40,
                "expected_username": "@RazakovVpnBot",
            },
            {
                "key": "razaltush",
                "token": "987654321:" + "B" * 40,
                "expected_username": "RazaltushVpnBot",
                "required": False,
            },
        ]
    )
    settings = _development_settings(TELEGRAM_BOT_TOKEN="", TELEGRAM_BOTS_JSON=payload)

    definitions = settings.telegram_bot_definitions()

    assert [definition.key for definition in definitions] == ["razakov", "razaltush"]
    assert definitions[0].expected_username == "RazakovVpnBot"
    assert "A" * 20 not in repr(definitions[0])


def test_log_formatter_emits_json_and_redacts_tokens_and_vpn_uris() -> None:
    record = logging.LogRecord(
        name="vpn_access_bot.test",
        level=logging.ERROR,
        pathname=__file__,
        lineno=1,
        msg=(
            "token=super-secret vless://user@example.test:443?token=hidden "
            "X-Admin-Token: admin-secret"
        ),
        args=(),
        exc_info=None,
    )
    record.correlation_id = "tg-razakov-42-test"
    record.bot_key = "razakov"

    payload = json.loads(JsonLineFormatter("VpnAccessBot").format(record))

    assert payload["service"] == "VpnAccessBot"
    assert payload["botKey"] == "razakov"
    assert payload["correlationId"] == "tg-razakov-42-test"
    assert "super-secret" not in payload["message"]
    assert "vless://" not in payload["message"]
    assert "admin-secret" not in payload["message"]
    assert redact_text("https://example.test/sub?token=value") == (
        "https://example.test/sub?token=[REDACTED]"
    )


def test_console_formatter_redacts_exception_and_subscription_url() -> None:
    formatter = RedactingFormatter("%(levelname)s %(message)s")
    try:
        raise RuntimeError("failed vless://user@example.test:443?token=secret")
    except RuntimeError:
        record = logging.LogRecord(
            name="vpn_access_bot.test",
            level=logging.ERROR,
            pathname=__file__,
            lineno=1,
            msg="Authorization: Bearer-secret",
            args=(),
            exc_info=__import__("sys").exc_info(),
        )

    formatted = formatter.format(record)

    assert "Bearer-secret" not in formatted
    assert "vless://" not in formatted
    assert "secret" not in formatted


def test_bounded_log_queue_drops_low_priority_and_synchronously_redacts_errors() -> None:
    import queue

    log_queue: queue.Queue[logging.LogRecord] = queue.Queue(maxsize=1)
    log_queue.put_nowait(
        logging.LogRecord("occupied", logging.INFO, __file__, 1, "occupied", (), None)
    )
    output = io.StringIO()
    emergency = logging.StreamHandler(output)
    emergency.setFormatter(RedactingFormatter("%(message)s"))
    handler = BoundedQueueHandler(log_queue, emergency)

    handler.emit(logging.LogRecord("drop", logging.INFO, __file__, 1, "drop", (), None))
    handler.emit(
        logging.LogRecord(
            "error",
            logging.ERROR,
            __file__,
            1,
            "token=super-secret",
            (),
            None,
        )
    )

    assert handler.dropped_records == 2
    assert "super-secret" not in output.getvalue()
    assert "[REDACTED]" in output.getvalue()


@pytest.mark.asyncio
async def test_same_telegram_user_is_shared_across_bot_channels(tmp_path: Path) -> None:
    database = Database(f"sqlite+aiosqlite:///{tmp_path / 'bot.db'}")
    await database.initialize()
    try:
        async with database.session() as session:
            channels = TelegramChannelRepository(session)
            now = datetime.now(UTC)
            await channels.register_bot(
                bot_key="razakov",
                telegram_bot_id=1001,
                username="RazakovVpnBot",
                enabled=True,
                required=True,
                verified_at_utc=now,
            )
            await channels.register_bot(
                bot_key="razaltush",
                telegram_bot_id=1002,
                username="RazaltushVpnBot",
                enabled=True,
                required=True,
                verified_at_utc=now,
            )

        for bot_key, bot_id in (("razakov", 1001), ("razaltush", 1002)):
            context_tokens = set_bot_context(bot_key, bot_id)
            try:
                async with database.session() as session:
                    await UserRepository(session).get_or_create_from_message_user(
                        telegram_id=777,
                        username="same-user",
                        first_name="User",
                    )
            finally:
                reset_bot_context(context_tokens)

        async with database.session() as session:
            users = await session.execute(select(func.count(User.id)))
            channels = await session.execute(select(func.count(UserBotChannel.bot_key)))

        assert users.scalar_one() == 1
        assert channels.scalar_one() == 2
    finally:
        await database.dispose()
