from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace

import pytest
from aiogram.dispatcher.event.bases import SkipHandler
from aiogram.types import Chat, Message
from aiogram.types import User as TelegramUser

from vpn_access_bot.handlers.common import (
    _support_unavailable_text,
    handle_support_admin_reply,
)


def _reply_message(
    *,
    chat_id: int,
    sender_id: int,
    chat_type: str,
) -> Message:
    chat = Chat(
        id=chat_id,
        type=chat_type,
    )

    return Message(
        message_id=11,
        date=datetime.now(UTC),
        chat=chat,
        from_user=TelegramUser(
            id=sender_id,
            is_bot=False,
            first_name="Sender",
        ),
        reply_to_message=Message(
            message_id=10,
            date=datetime.now(UTC),
            chat=chat,
            from_user=TelegramUser(
                id=999,
                is_bot=True,
                first_name="Bot",
            ),
            text="Previous message",
        ),
        text="Reply",
    )


@pytest.mark.asyncio
async def test_user_reply_is_passed_to_following_handler() -> None:
    settings = SimpleNamespace(
        support_chat_id=-1001234567890,
        support_agent_telegram_ids=[42],
    )

    message = _reply_message(
        chat_id=123456,
        sender_id=123456,
        chat_type="private",
    )

    with pytest.raises(SkipHandler):
        await handle_support_admin_reply(
            message,
            database=object(),
            settings=settings,
        )


@pytest.mark.asyncio
async def test_unauthorized_support_chat_reply_is_consumed() -> None:
    settings = SimpleNamespace(
        support_chat_id=-1001234567890,
        support_agent_telegram_ids=[42],
    )

    message = _reply_message(
        chat_id=settings.support_chat_id,
        sender_id=7,
        chat_type="supergroup",
    )

    result = await handle_support_admin_reply(
        message,
        database=object(),
        settings=settings,
    )

    assert result is None


def test_support_unavailable_message_includes_configured_fallback_contact() -> None:
    settings = SimpleNamespace(support_contact="@razaltush_support")

    text = _support_unavailable_text(settings)

    assert "@razaltush_support" in text
    assert "Напишите напрямую" in text


def test_support_unavailable_message_escapes_fallback_contact() -> None:
    settings = SimpleNamespace(support_contact="<support&team>")

    text = _support_unavailable_text(settings)

    assert "<support&team>" not in text
    assert "&lt;support&amp;team&gt;" in text
