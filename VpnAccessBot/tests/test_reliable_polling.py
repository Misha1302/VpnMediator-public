from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace

import pytest
from aiogram.types import (
    CallbackQuery,
    Chat,
    Message,
    PreCheckoutQuery,
    SuccessfulPayment,
    Update,
    User,
)
from sqlalchemy import func, select

from vpn_access_bot.config import Settings
from vpn_access_bot.db import Database
from vpn_access_bot.models import PaymentInbox, TelegramUpdateInbox
from vpn_access_bot.payment_processing import PaymentInboxIngestionService
from vpn_access_bot.telegram.reliable_polling import (
    ReliablePollingRunner,
    _is_terminal_callback_error,
    _is_terminal_query_error,
)
from vpn_access_bot.telegram.update_inbox import TelegramUpdateInboxRepository


class _Registry:
    def __init__(self) -> None:
        self.bot = None

    def resolve(self, bot):
        self.bot = bot
        return SimpleNamespace(key="primary")

    def get(self, bot_key):
        if bot_key != "primary" or self.bot is None:
            return None
        return SimpleNamespace(key="primary", bot=self.bot)


class _Dispatcher:
    def __init__(self) -> None:
        self.feed_calls: list[int] = []
        self.completed = False

    async def feed_update(self, bot, update, **workflow_data):
        _ = bot, workflow_data
        self.feed_calls.append(update.update_id)
        self.completed = True


class _SuccessfulPaymentBot:
    def __init__(
        self,
        database: Database,
        update: Update,
        *,
        delivery_count: int = 1,
        expect_payment: bool = True,
    ) -> None:
        self.database = database
        self.update = update
        self.delivery_count = delivery_count
        self.expect_payment = expect_payment
        self.offsets: list[int | None] = []

    async def get_updates(self, *, offset, **kwargs):
        _ = kwargs
        self.offsets.append(offset)
        if len(self.offsets) <= self.delivery_count:
            return [self.update]

        async with self.database.session() as session:
            count = await session.scalar(select(func.count(PaymentInbox.id)))
            assert count == (1 if self.expect_payment else 0)
            update_count = await session.scalar(select(func.count(TelegramUpdateInbox.id)))
            assert update_count == 1
        raise asyncio.CancelledError


class _ExpiredPreCheckoutBot:
    def __init__(self, update: Update) -> None:
        self.update = update
        self.offsets: list[int | None] = []

    async def get_updates(self, *, offset, **kwargs):
        _ = kwargs
        self.offsets.append(offset)
        if len(self.offsets) == 1:
            return [self.update]
        raise asyncio.CancelledError


class _PreCheckoutBot:
    def __init__(self, dispatcher: _Dispatcher, update: Update) -> None:
        self.dispatcher = dispatcher
        self.update = update
        self.offsets: list[int | None] = []

    async def get_updates(self, *, offset, **kwargs):
        _ = kwargs
        self.offsets.append(offset)
        if len(self.offsets) == 1:
            return [self.update]

        assert self.dispatcher.completed is True
        raise asyncio.CancelledError


def _settings() -> Settings:
    return Settings(
        TELEGRAM_BOT_TOKEN="test-token",
        MEDIATOR_ADMIN_TOKEN="test-admin-token",
        TELEGRAM_UPDATE_CONCURRENCY_LIMIT=2,
    )


def _payment_update(update_id: int = 25) -> Update:
    user = User(id=100, is_bot=False, first_name="Payer")
    return Update(
        update_id=update_id,
        message=Message(
            message_id=10,
            date=datetime.now(UTC),
            chat=Chat(id=100, type="private"),
            from_user=user,
            successful_payment=SuccessfulPayment(
                currency="XTR",
                total_amount=199,
                invoice_payload="order:test-payment",
                telegram_payment_charge_id="tg-test-charge",
                provider_payment_charge_id="provider-charge",
            ),
        ),
    )


def _pre_checkout_update(update_id: int = 30) -> Update:
    user = User(id=100, is_bot=False, first_name="Payer")
    return Update(
        update_id=update_id,
        pre_checkout_query=PreCheckoutQuery(
            id="checkout-id",
            from_user=user,
            currency="XTR",
            total_amount=199,
            invoice_payload="order:test-payment",
        ),
    )


def _runner(database: Database, dispatcher: _Dispatcher) -> ReliablePollingRunner:
    return ReliablePollingRunner(
        dispatcher=dispatcher,  # type: ignore[arg-type]
        registry=_Registry(),  # type: ignore[arg-type]
        database=database,
        settings=_settings(),
        workflow_data={},
        polling_timeout_seconds=1,
    )


@pytest.mark.asyncio
async def test_successful_payment_is_committed_before_offset_advances(tmp_path: Path) -> None:
    database = Database(f"sqlite+aiosqlite:///{tmp_path / 'polling-payment.db'}")
    await database.initialize()
    dispatcher = _Dispatcher()
    update = _payment_update()
    bot = _SuccessfulPaymentBot(database, update)
    runner = _runner(database, dispatcher)

    try:
        with pytest.raises(asyncio.CancelledError):
            await runner._poll_bot(bot, [], {})  # type: ignore[arg-type]
        assert bot.offsets == [None, update.update_id + 1]

        async with database.session() as session:
            inbox = (await session.execute(select(PaymentInbox))).scalar_one()
            assert inbox.provider_charge_id == "tg-test-charge"
            assert inbox.origin_bot_key == "primary"
            assert inbox.reconciliation_status == "received"
            update_inbox = (await session.execute(select(TelegramUpdateInbox))).scalar_one()
            assert update_inbox.update_id == update.update_id
            assert update_inbox.status == "pending"
    finally:
        await runner._cancel_pending_updates()
        await database.dispose()


@pytest.mark.asyncio
async def test_payment_ingestion_failure_retries_without_advancing_offset(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database = Database(f"sqlite+aiosqlite:///{tmp_path / 'polling-failure.db'}")
    await database.initialize()
    dispatcher = _Dispatcher()
    update = _payment_update()
    bot = _SuccessfulPaymentBot(database, update, delivery_count=2)
    runner = _runner(database, dispatcher)
    original = PaymentInboxIngestionService.ingest_telegram_stars
    attempts = 0

    async def fail_once(self, evidence):
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise RuntimeError("database unavailable")
        return await original(self, evidence)

    async def no_sleep(delay):
        _ = delay

    monkeypatch.setattr(PaymentInboxIngestionService, "ingest_telegram_stars", fail_once)
    monkeypatch.setattr(asyncio, "sleep", no_sleep)

    try:
        with pytest.raises(asyncio.CancelledError):
            await runner._poll_bot(bot, [], {})  # type: ignore[arg-type]
        assert attempts == 2
        assert bot.offsets == [None, None, update.update_id + 1]
        assert dispatcher.feed_calls == []
    finally:
        await runner._cancel_pending_updates()
        await database.dispose()


@pytest.mark.asyncio
async def test_ordinary_update_is_persisted_before_offset_advances(tmp_path: Path) -> None:
    database = Database(f"sqlite+aiosqlite:///{tmp_path / 'polling-ordinary.db'}")
    await database.initialize()
    dispatcher = _Dispatcher()
    update = Update(
        update_id=41,
        message=Message(
            message_id=11,
            date=datetime.now(UTC),
            chat=Chat(id=100, type="private"),
            from_user=User(id=100, is_bot=False, first_name="User"),
            text="/start",
        ),
    )
    bot = _SuccessfulPaymentBot(database, update, expect_payment=False)
    runner = _runner(database, dispatcher)

    try:
        with pytest.raises(asyncio.CancelledError):
            await runner._poll_bot(bot, [], {})  # type: ignore[arg-type]
        assert bot.offsets == [None, update.update_id + 1]
        assert dispatcher.feed_calls == []
        async with database.session() as session:
            inbox = (await session.execute(select(TelegramUpdateInbox))).scalar_one()
            assert inbox.bot_key == "primary"
            assert inbox.update_id == update.update_id
            assert inbox.status == "pending"
    finally:
        await runner._cancel_pending_updates()
        await database.dispose()


@pytest.mark.asyncio
async def test_pre_checkout_is_fully_handled_before_offset_advances(tmp_path: Path) -> None:
    database = Database(f"sqlite+aiosqlite:///{tmp_path / 'polling-checkout.db'}")
    await database.initialize()
    dispatcher = _Dispatcher()
    update = _pre_checkout_update()
    bot = _PreCheckoutBot(dispatcher, update)
    runner = _runner(database, dispatcher)

    try:
        with pytest.raises(asyncio.CancelledError):
            await runner._poll_bot(bot, [], {})  # type: ignore[arg-type]
        assert dispatcher.feed_calls == [update.update_id]
        assert bot.offsets == [None, update.update_id + 1]
    finally:
        await runner._cancel_pending_updates()
        await database.dispose()


@pytest.mark.asyncio
async def test_expired_pre_checkout_advances_offset_instead_of_retry_loop(
    tmp_path: Path,
) -> None:
    from aiogram.exceptions import TelegramBadRequest
    from aiogram.methods import AnswerPreCheckoutQuery

    database = Database(f"sqlite+aiosqlite:///{tmp_path / 'polling-expired-checkout.db'}")
    await database.initialize()
    exception = TelegramBadRequest(
        method=AnswerPreCheckoutQuery(
            pre_checkout_query_id="checkout-id",
            ok=False,
        ),
        message=(
            "Bad Request: query is too old and response timeout expired or query ID is invalid"
        ),
    )
    dispatcher = _FailingDispatcher(exception)
    update = _pre_checkout_update()
    bot = _ExpiredPreCheckoutBot(update)
    runner = _runner(database, dispatcher)

    try:
        with pytest.raises(asyncio.CancelledError):
            await runner._poll_bot(bot, [], {})  # type: ignore[arg-type]
        assert dispatcher.feed_calls == [update.update_id]
        assert bot.offsets == [None, update.update_id + 1]
    finally:
        await runner._cancel_pending_updates()
        await database.dispose()


class _FailingDispatcher(_Dispatcher):
    def __init__(self, exception: Exception) -> None:
        super().__init__()
        self.exception = exception

    async def feed_update(self, bot, update, **workflow_data):
        _ = bot, workflow_data
        self.feed_calls.append(update.update_id)
        raise self.exception


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "message",
    [
        "Bad Request: query is too old and response timeout expired or query ID is invalid",
        "Bad Request: query ID is invalid",
    ],
)
async def test_expired_callback_is_marked_processed(
    tmp_path: Path,
    message: str,
) -> None:
    from aiogram.exceptions import TelegramBadRequest
    from aiogram.methods import AnswerCallbackQuery

    database = Database(f"sqlite+aiosqlite:///{tmp_path / 'polling-stale-callback.db'}")
    await database.initialize()
    dispatcher = _FailingDispatcher(
        TelegramBadRequest(
            method=AnswerCallbackQuery(callback_query_id="callback-id"),
            message=message,
        )
    )
    runner = _runner(database, dispatcher)
    bot = object()
    runner._registry.resolve(bot)  # type: ignore[arg-type]
    callback_message = Message(
        message_id=11,
        date=datetime.now(UTC),
        chat=Chat(id=100, type="private"),
        from_user=User(id=100, is_bot=False, first_name="User"),
        text="menu",
    )
    update = Update(
        update_id=77,
        callback_query=CallbackQuery(
            id="callback-id",
            from_user=User(id=100, is_bot=False, first_name="User"),
            chat_instance="chat-instance",
            message=callback_message,
            data="action",
        ),
    )

    try:
        await runner._durably_ingest_update(bot, update)  # type: ignore[arg-type]
        async with database.session() as session:
            claimed = await TelegramUpdateInboxRepository(session).claim_due(
                worker_id="worker-1",
                limit=1,
                lease_seconds=30,
            )
        assert len(claimed) == 1

        await runner._process_claimed_update(claimed[0], "worker-1", {})

        async with database.session() as session:
            inbox = (await session.execute(select(TelegramUpdateInbox))).scalar_one()
            assert inbox.status == "processed"
            assert inbox.payload_json == '{"redacted":true}'
            assert inbox.attempt_count == 1
            assert inbox.failure_code is None
            assert inbox.last_error_message is None
    finally:
        await runner._cancel_pending_updates()
        await database.dispose()


@pytest.mark.asyncio
async def test_message_not_modified_is_not_globally_swallowed(tmp_path: Path) -> None:
    from aiogram.exceptions import TelegramBadRequest
    from aiogram.methods import EditMessageText

    database = Database(f"sqlite+aiosqlite:///{tmp_path / 'polling-message-not-modified.db'}")
    await database.initialize()
    dispatcher = _FailingDispatcher(
        TelegramBadRequest(
            method=EditMessageText(text="same", chat_id=100, message_id=11),
            message="Bad Request: message is not modified",
        )
    )
    runner = _runner(database, dispatcher)
    bot = object()
    runner._registry.resolve(bot)  # type: ignore[arg-type]
    update = Update(
        update_id=78,
        message=Message(
            message_id=11,
            date=datetime.now(UTC),
            chat=Chat(id=100, type="private"),
            from_user=User(id=100, is_bot=False, first_name="User"),
            text="/start",
        ),
    )

    try:
        await runner._durably_ingest_update(bot, update)  # type: ignore[arg-type]
        async with database.session() as session:
            claimed = await TelegramUpdateInboxRepository(session).claim_due(
                worker_id="worker-1", limit=1, lease_seconds=30
            )
        await runner._process_claimed_update(claimed[0], "worker-1", {})

        async with database.session() as session:
            inbox = (await session.execute(select(TelegramUpdateInbox))).scalar_one()
            assert inbox.status == "retry"
            assert inbox.failure_code == "TelegramBadRequest"
    finally:
        await runner._cancel_pending_updates()
        await database.dispose()


def test_other_telegram_bad_request_remains_retryable() -> None:
    from aiogram.exceptions import TelegramBadRequest
    from aiogram.methods import GetMe

    exception = TelegramBadRequest(method=GetMe(), message="Bad Request: chat not found")

    assert _is_terminal_query_error(exception) is False
    assert _is_terminal_callback_error(_pre_checkout_update(), exception) is False
