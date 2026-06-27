from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest
from aiogram.types import CallbackQuery, Chat, Message, Update, User

from vpn_access_bot.user_operation_serialization import UserOperationSerializationMiddleware


def _callback_update(update_id: int, user_id: int) -> Update:
    actor = User(id=user_id, is_bot=False, first_name="User")
    message = Message(
        message_id=update_id,
        date=0,
        chat=Chat(id=user_id, type="private"),
        from_user=actor,
        text="menu",
    )
    callback = CallbackQuery(
        id=str(update_id),
        from_user=actor,
        chat_instance="chat",
        message=message,
        data="state:change",
    )
    return Update(update_id=update_id, callback_query=callback)


@pytest.mark.asyncio
async def test_serializes_same_bot_and_user() -> None:
    middleware = UserOperationSerializationMiddleware()
    active = 0
    maximum_active = 0

    async def handler(event, data):
        nonlocal active, maximum_active
        active += 1
        maximum_active = max(maximum_active, active)
        await asyncio.sleep(0.02)
        active -= 1
        return SimpleNamespace(event=event, data=data)

    await asyncio.gather(
        middleware(handler, _callback_update(1, 7), {"bot_key": "main"}),
        middleware(handler, _callback_update(2, 7), {"bot_key": "main"}),
    )

    assert maximum_active == 1
    assert middleware._entries == {}


@pytest.mark.asyncio
async def test_allows_different_users_to_progress_concurrently() -> None:
    middleware = UserOperationSerializationMiddleware()
    entered = asyncio.Event()
    release = asyncio.Event()
    active = 0

    async def handler(event, data):
        nonlocal active
        active += 1
        if active == 2:
            entered.set()
        await release.wait()
        active -= 1

    tasks = [
        asyncio.create_task(middleware(handler, _callback_update(1, 7), {"bot_key": "main"})),
        asyncio.create_task(middleware(handler, _callback_update(2, 8), {"bot_key": "main"})),
    ]
    await asyncio.wait_for(entered.wait(), timeout=1)
    release.set()
    await asyncio.gather(*tasks)

    assert middleware._entries == {}
