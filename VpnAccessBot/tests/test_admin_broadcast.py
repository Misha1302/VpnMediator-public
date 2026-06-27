from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass, field
from datetime import timedelta
from types import SimpleNamespace
from typing import Any

import pytest
from aiogram.enums import ChatType
from aiogram.exceptions import TelegramBadRequest
from sqlalchemy import func, select

from vpn_access_bot.broadcast import (
    BroadcastCommandError,
    BroadcastRequest,
    BroadcastService,
    parse_broadcast_command,
    parse_broadcast_confirmation,
)
from vpn_access_bot.config import Settings
from vpn_access_bot.db import Database
from vpn_access_bot.handlers.admin import _handle_broadcast, handle_broadcast_confirm
from vpn_access_bot.models import (
    AuditEvent,
    BroadcastCampaign,
    BroadcastRecipient,
    NotificationOutbox,
    User,
    utc_now,
)
from vpn_access_bot.product_completion import dispatch_notification_outbox_once
from vpn_access_bot.repositories import NotificationOutboxRepository, to_aware_utc
from vpn_access_bot.telegram.context import reset_bot_context, set_bot_context
from vpn_access_bot.telegram.notification_sender import NotificationRecipientUnavailable


@dataclass
class FakeAdminMessage:
    text: str
    sender_id: int
    message_id: int = 10
    chat_id: int | None = None
    chat_type: ChatType = ChatType.PRIVATE
    answers: list[str] = field(default_factory=list)

    def __post_init__(self) -> None:
        if self.chat_id is None:
            self.chat_id = self.sender_id

    @property
    def from_user(self) -> Any:
        return SimpleNamespace(id=self.sender_id)

    @property
    def chat(self) -> Any:
        return SimpleNamespace(id=self.chat_id, type=self.chat_type)

    async def answer(self, text: str, **_: object) -> None:
        self.answers.append(text)


def _settings() -> Settings:
    return Settings(
        TELEGRAM_BOT_TOKEN="test-token",
        MEDIATOR_ADMIN_TOKEN="test-admin-token",
        ADMIN_TELEGRAM_IDS="11",
        BROADCAST_CONFIRMATION_TTL_MINUTES=15,
        BROADCAST_MAX_DELIVERY_ATTEMPTS=2,
        BROADCAST_RETRY_BASE_SECONDS=30,
        BROADCAST_RETRY_MAX_SECONDS=300,
    )


async def _add_users(database: Database, telegram_ids: list[int]) -> None:
    now = utc_now()
    async with database.session() as session:
        session.add_all(
            [
                User(
                    telegram_id=telegram_id,
                    referral_code=f"broadcast-{telegram_id}",
                    created_at=now,
                    updated_at=now,
                )
                for telegram_id in telegram_ids
            ]
        )


def test_broadcast_parser_preserves_multiline_body_and_requires_preview_syntax() -> None:
    request = parse_broadcast_command(
        "/broadcast\nПервая строка\n\nТретья строка",
        filtered=False,
    )

    assert request.pattern is None
    assert request.message_text == "Первая строка\n\nТретья строка"

    filtered = parse_broadcast_command(
        "/broadcast_regex ^12\\d+$\nСообщение",
        filtered=True,
    )
    assert filtered.pattern == r"^12\d+$"

    with pytest.raises(BroadcastCommandError, match="invalid_command"):
        parse_broadcast_command("/broadcast CONFIRM\nТекст", filtered=False)
    with pytest.raises(BroadcastCommandError, match="invalid_regex"):
        parse_broadcast_command("/broadcast_regex [\nТекст", filtered=True)
    with pytest.raises(BroadcastCommandError, match="message_too_long"):
        parse_broadcast_command("/broadcast\n" + "x" * 4097, filtered=False)
    with pytest.raises(BroadcastCommandError, match="unsafe_regex"):
        parse_broadcast_command(r"/broadcast_regex ^(\d+)+$\nТекст", filtered=True)
    with pytest.raises(BroadcastCommandError, match="unsafe_regex"):
        parse_broadcast_command(r"/broadcast_regex ^(\d)\1+$\nТекст", filtered=True)


def test_confirmation_parser_rejects_missing_and_malformed_tokens() -> None:
    assert parse_broadcast_confirmation("/broadcast_confirm abcdefghijklmnop") == (
        "abcdefghijklmnop"
    )
    with pytest.raises(BroadcastCommandError, match="confirmation_token_required"):
        parse_broadcast_confirmation("/broadcast_confirm")
    with pytest.raises(BroadcastCommandError, match="confirmation_token_invalid"):
        parse_broadcast_confirmation("/broadcast_confirm short")
    with pytest.raises(BroadcastCommandError, match="confirmation_token_invalid"):
        parse_broadcast_confirmation("/broadcast_confirm invalid.token.value")


@pytest.mark.asyncio
async def test_prepare_snapshots_recipients_without_creating_outbox_or_auditing_body(
    tmp_path,
) -> None:
    database = Database(f"sqlite+aiosqlite:///{tmp_path / 'bot.db'}")
    await database.initialize()
    await _add_users(database, [123, 456])

    try:
        preview = await BroadcastService(database.session).prepare(
            BroadcastRequest(message_text="Секретов здесь нет\nВторая строка"),
            admin_telegram_id=11,
            source_chat_id=11,
            source_message_id=77,
            confirmation_ttl_minutes=15,
        )

        assert preview is not None
        assert preview.target_count == 2
        async with database.session() as session:
            campaign = (await session.execute(select(BroadcastCampaign))).scalar_one()
            recipients = list((await session.execute(select(BroadcastRecipient))).scalars())
            outbox_count = int(
                (await session.scalar(select(func.count(NotificationOutbox.id)))) or 0
            )
            audit = (
                await session.execute(
                    select(AuditEvent).where(AuditEvent.event_type == "admin.broadcast.prepared")
                )
            ).scalar_one()

        assert campaign.state == "awaiting_confirmation"
        assert campaign.message_text == "Секретов здесь нет\nВторая строка"
        assert len(recipients) == 2
        assert outbox_count == 0
        assert "Секретов здесь нет" not in (audit.details_json or "")
    finally:
        await database.dispose()


@pytest.mark.asyncio
async def test_repeated_prepare_rotates_confirmation_token_after_response_loss(tmp_path) -> None:
    database = Database(f"sqlite+aiosqlite:///{tmp_path / 'bot.db'}")
    await database.initialize()
    await _add_users(database, [123, 456])

    try:
        service = BroadcastService(database.session)
        first = await service.prepare(
            BroadcastRequest(message_text="Важное сообщение"),
            admin_telegram_id=11,
            source_chat_id=11,
            source_message_id=770,
            confirmation_ttl_minutes=15,
        )
        second = await service.prepare(
            BroadcastRequest(message_text="Важное сообщение"),
            admin_telegram_id=11,
            source_chat_id=11,
            source_message_id=770,
            confirmation_ttl_minutes=15,
        )

        assert first is not None
        assert second is not None
        assert second.campaign_id == first.campaign_id
        assert second.target_count == first.target_count == 2
        assert second.confirmation_token != first.confirmation_token

        with pytest.raises(BroadcastCommandError, match="confirmation_token_invalid"):
            await service.confirm_and_enqueue(
                first.confirmation_token,
                admin_telegram_id=11,
                source_chat_id=11,
            )

        result = await service.confirm_and_enqueue(
            second.confirmation_token,
            admin_telegram_id=11,
            source_chat_id=11,
        )
        assert result.target_count == 2

        async with database.session() as session:
            campaign_count = int(
                (await session.scalar(select(func.count(BroadcastCampaign.id)))) or 0
            )
            recipient_count = int(
                (await session.scalar(select(func.count(BroadcastRecipient.user_id)))) or 0
            )
            rotation_audit = (
                await session.execute(
                    select(AuditEvent).where(
                        AuditEvent.event_type == "admin.broadcast.confirmation_rotated"
                    )
                )
            ).scalar_one()

        assert campaign_count == 1
        assert recipient_count == 2
        assert "Важное сообщение" not in (rotation_audit.details_json or "")
    finally:
        await database.dispose()


@pytest.mark.asyncio
async def test_regex_snapshot_uses_fullmatch_and_freezes_upper_user_bound(tmp_path) -> None:
    database = Database(f"sqlite+aiosqlite:///{tmp_path / 'bot.db'}")
    await database.initialize()
    await _add_users(database, [123, 1234, 9123])

    try:
        preview = await BroadcastService(database.session).prepare(
            BroadcastRequest(message_text="Текст", pattern=r"^123\d?$"),
            admin_telegram_id=11,
            source_chat_id=11,
            source_message_id=78,
            confirmation_ttl_minutes=15,
        )
        assert preview is not None
        assert preview.target_count == 2

        await _add_users(database, [12345])
        result = await BroadcastService(database.session).confirm_and_enqueue(
            preview.confirmation_token,
            admin_telegram_id=11,
            source_chat_id=11,
        )
        assert result.target_count == 2
        async with database.session() as session:
            telegram_ids = list(
                (
                    await session.execute(
                        select(User.telegram_id)
                        .join(NotificationOutbox, NotificationOutbox.user_id == User.id)
                        .order_by(User.telegram_id)
                    )
                ).scalars()
            )
        assert telegram_ids == [123, 1234]
    finally:
        await database.dispose()


@pytest.mark.asyncio
async def test_confirmation_enqueues_campaign_references_without_copying_message_text(
    tmp_path,
) -> None:
    database = Database(f"sqlite+aiosqlite:///{tmp_path / 'bot.db'}")
    await database.initialize()
    await _add_users(database, [123, 456])

    try:
        preview = await BroadcastService(database.session).prepare(
            BroadcastRequest(message_text="Первая строка\nВторая строка"),
            admin_telegram_id=11,
            source_chat_id=11,
            source_message_id=79,
            confirmation_ttl_minutes=15,
        )
        assert preview is not None

        result = await BroadcastService(database.session).confirm_and_enqueue(
            preview.confirmation_token,
            admin_telegram_id=11,
            source_chat_id=11,
        )

        assert result.target_count == 2
        assert result.newly_queued_count == 2
        async with database.session() as session:
            campaign = (await session.execute(select(BroadcastCampaign))).scalar_one()
            rows = list(
                (
                    await session.execute(
                        select(NotificationOutbox).order_by(NotificationOutbox.user_id)
                    )
                ).scalars()
            )

        assert campaign.state == "queued"
        assert campaign.queued_count == 2
        assert all(row.broadcast_campaign_id == campaign.id for row in rows)
        assert all("message_text" not in (row.payload_json or "") for row in rows)
        assert all(preview.campaign_id in (row.payload_json or "") for row in rows)
    finally:
        await database.dispose()


@pytest.mark.asyncio
async def test_confirmation_token_is_bound_to_admin_and_private_chat(tmp_path) -> None:
    database = Database(f"sqlite+aiosqlite:///{tmp_path / 'bot.db'}")
    await database.initialize()
    await _add_users(database, [123])

    try:
        preview = await BroadcastService(database.session).prepare(
            BroadcastRequest(message_text="Текст"),
            admin_telegram_id=11,
            source_chat_id=11,
            source_message_id=80,
            confirmation_ttl_minutes=15,
        )
        assert preview is not None

        with pytest.raises(BroadcastCommandError, match="confirmation_token_invalid"):
            await BroadcastService(database.session).confirm_and_enqueue(
                preview.confirmation_token,
                admin_telegram_id=12,
                source_chat_id=11,
            )
        with pytest.raises(BroadcastCommandError, match="confirmation_token_invalid"):
            await BroadcastService(database.session).confirm_and_enqueue(
                preview.confirmation_token,
                admin_telegram_id=11,
                source_chat_id=999,
            )
    finally:
        await database.dispose()


@pytest.mark.asyncio
async def test_expired_confirmation_cannot_enqueue(tmp_path) -> None:
    database = Database(f"sqlite+aiosqlite:///{tmp_path / 'bot.db'}")
    await database.initialize()
    await _add_users(database, [123])

    try:
        preview = await BroadcastService(database.session).prepare(
            BroadcastRequest(message_text="Текст"),
            admin_telegram_id=11,
            source_chat_id=11,
            source_message_id=81,
            confirmation_ttl_minutes=15,
        )
        assert preview is not None
        async with database.session() as session:
            campaign = (await session.execute(select(BroadcastCampaign))).scalar_one()
            campaign.expires_at_utc = utc_now() - timedelta(seconds=1)

        with pytest.raises(BroadcastCommandError, match="confirmation_token_expired"):
            await BroadcastService(database.session).confirm_and_enqueue(
                preview.confirmation_token,
                admin_telegram_id=11,
                source_chat_id=11,
            )
        async with database.session() as session:
            assert (await session.execute(select(BroadcastCampaign))).scalar_one().state == (
                "expired"
            )
            assert int(await session.scalar(select(func.count(NotificationOutbox.id))) or 0) == 0
    finally:
        await database.dispose()


@pytest.mark.asyncio
async def test_parallel_confirmation_is_idempotent(tmp_path) -> None:
    database = Database(f"sqlite+aiosqlite:///{tmp_path / 'bot.db'}")
    await database.initialize()
    await _add_users(database, [123, 456])

    try:
        preview = await BroadcastService(database.session).prepare(
            BroadcastRequest(message_text="Одно сообщение"),
            admin_telegram_id=11,
            source_chat_id=11,
            source_message_id=82,
            confirmation_ttl_minutes=15,
        )
        assert preview is not None

        results = await asyncio.gather(
            *[
                BroadcastService(database.session).confirm_and_enqueue(
                    preview.confirmation_token,
                    admin_telegram_id=11,
                    source_chat_id=11,
                )
                for _ in range(2)
            ]
        )

        assert sum(result.newly_queued_count for result in results) == 2
        async with database.session() as session:
            assert int(await session.scalar(select(func.count(NotificationOutbox.id))) or 0) == 2
            campaign = (await session.execute(select(BroadcastCampaign))).scalar_one()
            assert campaign.state == "queued"
            assert campaign.queued_count == 2
    finally:
        await database.dispose()


@pytest.mark.asyncio
async def test_large_broadcast_is_snapshotted_and_queued_in_batches(tmp_path) -> None:
    database = Database(f"sqlite+aiosqlite:///{tmp_path / 'bot.db'}")
    await database.initialize()
    await _add_users(database, list(range(100_000, 100_620)))

    try:
        preview = await BroadcastService(database.session).prepare(
            BroadcastRequest(message_text="Пакетная рассылка"),
            admin_telegram_id=11,
            source_chat_id=11,
            source_message_id=83,
            confirmation_ttl_minutes=15,
        )
        assert preview is not None
        assert preview.target_count == 620

        result = await BroadcastService(database.session).confirm_and_enqueue(
            preview.confirmation_token,
            admin_telegram_id=11,
            source_chat_id=11,
        )
        assert result.newly_queued_count == 620
        async with database.session() as session:
            assert int(await session.scalar(select(func.count(NotificationOutbox.id))) or 0) == 620
    finally:
        await database.dispose()


@pytest.mark.asyncio
async def test_admin_handler_requires_preview_then_separate_confirmation(tmp_path) -> None:
    database = Database(f"sqlite+aiosqlite:///{tmp_path / 'bot.db'}")
    await database.initialize()
    await _add_users(database, [123])
    settings = _settings()
    prepare_message = FakeAdminMessage(
        text="/broadcast_regex ^123$\nПервая строка\nВторая строка",
        sender_id=11,
        message_id=84,
    )

    try:
        await _handle_broadcast(
            prepare_message,  # type: ignore[arg-type]
            database,
            settings,
            filtered=True,
        )
        assert len(prepare_message.answers) == 1
        assert "Проверьте рассылку" in prepare_message.answers[0]
        token = prepare_message.answers[0].split("/broadcast_confirm ", 1)[1].split("<", 1)[0]

        async with database.session() as session:
            assert int(await session.scalar(select(func.count(NotificationOutbox.id))) or 0) == 0

        confirm_message = FakeAdminMessage(
            text=f"/broadcast_confirm {token}",
            sender_id=11,
            message_id=85,
        )
        await handle_broadcast_confirm(
            confirm_message,  # type: ignore[arg-type]
            database,
            settings,
        )
        assert len(confirm_message.answers) == 1
        assert "Рассылка поставлена в очередь" in confirm_message.answers[0]
    finally:
        await database.dispose()


@pytest.mark.asyncio
async def test_non_admin_and_group_handler_cannot_prepare_broadcast(tmp_path) -> None:
    database = Database(f"sqlite+aiosqlite:///{tmp_path / 'bot.db'}")
    await database.initialize()
    await _add_users(database, [123])
    settings = _settings()

    try:
        non_admin = FakeAdminMessage(text="/broadcast\nТекст", sender_id=12)
        group_admin = FakeAdminMessage(
            text="/broadcast\nТекст",
            sender_id=11,
            chat_id=-100,
            chat_type=ChatType.SUPERGROUP,
        )
        await _handle_broadcast(
            non_admin,  # type: ignore[arg-type]
            database,
            settings,
            filtered=False,
        )
        await _handle_broadcast(
            group_admin,  # type: ignore[arg-type]
            database,
            settings,
            filtered=False,
        )
        assert non_admin.answers == []
        assert group_admin.answers == []
        async with database.session() as session:
            assert int(await session.scalar(select(func.count(BroadcastCampaign.id))) or 0) == 0
    finally:
        await database.dispose()


@pytest.mark.asyncio
async def test_same_admin_message_identity_is_isolated_per_bot(tmp_path) -> None:
    database = Database(f"sqlite+aiosqlite:///{tmp_path / 'bot.db'}")
    await database.initialize()
    await _add_users(database, [123])

    try:
        primary_tokens = set_bot_context("primary", 1001)
        try:
            primary = await BroadcastService(database.session).prepare(
                BroadcastRequest(message_text="Primary"),
                admin_telegram_id=11,
                source_chat_id=11,
                source_message_id=500,
                confirmation_ttl_minutes=15,
            )
        finally:
            reset_bot_context(primary_tokens)

        secondary_tokens = set_bot_context("secondary", 1002)
        try:
            secondary = await BroadcastService(database.session).prepare(
                BroadcastRequest(message_text="Secondary"),
                admin_telegram_id=11,
                source_chat_id=11,
                source_message_id=500,
                confirmation_ttl_minutes=15,
            )
            assert secondary is not None
            with pytest.raises(BroadcastCommandError, match="confirmation_token_invalid"):
                await BroadcastService(database.session).confirm_and_enqueue(
                    primary.confirmation_token if primary is not None else "",
                    admin_telegram_id=11,
                    source_chat_id=11,
                )
            secondary_result = await BroadcastService(database.session).confirm_and_enqueue(
                secondary.confirmation_token,
                admin_telegram_id=11,
                source_chat_id=11,
            )
        finally:
            reset_bot_context(secondary_tokens)

        assert primary is not None
        assert secondary_result.target_count == 1

        primary_tokens = set_bot_context("primary", 1001)
        try:
            primary_result = await BroadcastService(database.session).confirm_and_enqueue(
                primary.confirmation_token,
                admin_telegram_id=11,
                source_chat_id=11,
            )
        finally:
            reset_bot_context(primary_tokens)

        assert primary_result.target_count == 1
        async with database.session() as session:
            campaigns = list(
                (
                    await session.execute(
                        select(BroadcastCampaign).order_by(BroadcastCampaign.source_bot_key)
                    )
                ).scalars()
            )
            outbox = list(
                (
                    await session.execute(
                        select(NotificationOutbox).order_by(NotificationOutbox.bot_key)
                    )
                ).scalars()
            )

        assert [campaign.source_bot_key for campaign in campaigns] == ["primary", "secondary"]
        assert [item.bot_key for item in outbox] == ["primary", "secondary"]
    finally:
        await database.dispose()


@pytest.mark.asyncio
async def test_campaign_outbox_uses_plain_text_and_marks_provider_accepted(tmp_path) -> None:
    database = Database(f"sqlite+aiosqlite:///{tmp_path / 'bot.db'}")
    await database.initialize()
    await _add_users(database, [777])

    class FakeSender:
        def __init__(self) -> None:
            self.calls: list[tuple[int, str, dict[str, object]]] = []

        async def send_message(self, telegram_id: int, text: str, **kwargs: object):
            self.calls.append((telegram_id, text, kwargs))
            return SimpleNamespace(delivery_bot_key="primary")

    try:
        preview = await BroadcastService(database.session).prepare(
            BroadcastRequest(message_text="<b>буквальный текст</b>\nстрока"),
            admin_telegram_id=11,
            source_chat_id=11,
            source_message_id=86,
            confirmation_ttl_minutes=15,
        )
        assert preview is not None
        await BroadcastService(database.session).confirm_and_enqueue(
            preview.confirmation_token,
            admin_telegram_id=11,
            source_chat_id=11,
        )
        sender = FakeSender()
        accepted = await dispatch_notification_outbox_once(
            database.session,
            sender,  # type: ignore[arg-type]
            _settings(),
        )

        assert accepted == 1
        assert sender.calls == [
            (
                777,
                "<b>буквальный текст</b>\nстрока",
                {"bot_key": "default", "parse_mode": None},
            )
        ]
        async with database.session() as session:
            item = (await session.execute(select(NotificationOutbox))).scalar_one()
            assert item.state == "provider_accepted"
    finally:
        await database.dispose()


@pytest.mark.asyncio
async def test_legacy_broadcast_outbox_remains_deliverable(tmp_path) -> None:
    database = Database(f"sqlite+aiosqlite:///{tmp_path / 'bot.db'}")
    await database.initialize()
    await _add_users(database, [778])
    now = utc_now()

    class FakeSender:
        def __init__(self) -> None:
            self.calls: list[str] = []

        async def send_message(self, _telegram_id: int, text: str, **_kwargs: object):
            self.calls.append(text)
            return SimpleNamespace(delivery_bot_key="primary")

    try:
        async with database.session() as session:
            user = (await session.execute(select(User))).scalar_one()
            session.add(
                NotificationOutbox(
                    public_id="00000000-0000-0000-0000-000000000778",
                    idempotency_key="legacy-broadcast",
                    user_id=user.id,
                    notification_kind="admin_broadcast",
                    payload_json=json.dumps({"message_text": "Старое сообщение"}),
                    state="pending",
                    attempt_count=0,
                    available_at_utc=now,
                    created_at_utc=now,
                    updated_at_utc=now,
                )
            )
        sender = FakeSender()
        accepted = await dispatch_notification_outbox_once(
            database.session,
            sender,  # type: ignore[arg-type]
            _settings(),
        )
        assert accepted == 1
        assert sender.calls == ["Старое сообщение"]
    finally:
        await database.dispose()


@pytest.mark.asyncio
async def test_broadcast_recipient_unavailable_and_bad_request_are_terminal(tmp_path) -> None:
    database = Database(f"sqlite+aiosqlite:///{tmp_path / 'bot.db'}")
    await database.initialize()
    await _add_users(database, [888, 889])
    now = utc_now()

    class UnavailableSender:
        async def send_message(self, telegram_id: int, _text: str, **_kwargs: object):
            if telegram_id == 888:
                raise NotificationRecipientUnavailable("blocked")
            raise TelegramBadRequest(
                method=object(),  # type: ignore[arg-type]
                message="Bad Request: invalid recipient",
            )

    try:
        async with database.session() as session:
            users = list((await session.execute(select(User).order_by(User.id))).scalars())
            for index, user in enumerate(users):
                session.add(
                    NotificationOutbox(
                        public_id=f"00000000-0000-0000-0000-{index + 1:012d}",
                        idempotency_key=f"terminal-{index}",
                        user_id=user.id,
                        notification_kind="admin_broadcast",
                        payload_json=json.dumps({"message_text": "Текст"}),
                        state="pending",
                        attempt_count=0,
                        available_at_utc=now,
                        created_at_utc=now,
                        updated_at_utc=now,
                    )
                )
        assert (
            await dispatch_notification_outbox_once(
                database.session,
                UnavailableSender(),  # type: ignore[arg-type]
                _settings(),
            )
            == 0
        )
        async with database.session() as session:
            rows = list(
                (
                    await session.execute(
                        select(NotificationOutbox).order_by(NotificationOutbox.user_id)
                    )
                ).scalars()
            )
            assert [row.state for row in rows] == ["terminal_failed", "terminal_failed"]
    finally:
        await database.dispose()


@pytest.mark.asyncio
async def test_transient_broadcast_failures_use_backoff_and_stop_after_limit(tmp_path) -> None:
    database = Database(f"sqlite+aiosqlite:///{tmp_path / 'bot.db'}")
    await database.initialize()
    await _add_users(database, [999])
    now = utc_now()

    class TransientFailureSender:
        async def send_message(self, _telegram_id: int, _text: str, **_kwargs: object):
            raise RuntimeError("temporary")

    try:
        async with database.session() as session:
            user = (await session.execute(select(User))).scalar_one()
            session.add(
                NotificationOutbox(
                    public_id="00000000-0000-0000-0000-000000000999",
                    idempotency_key="transient-broadcast",
                    user_id=user.id,
                    notification_kind="admin_broadcast",
                    payload_json=json.dumps({"message_text": "Текст"}),
                    state="pending",
                    attempt_count=0,
                    available_at_utc=now,
                    created_at_utc=now,
                    updated_at_utc=now,
                )
            )

        await dispatch_notification_outbox_once(
            database.session,
            TransientFailureSender(),  # type: ignore[arg-type]
            _settings(),
        )
        async with database.session() as session:
            item = (await session.execute(select(NotificationOutbox))).scalar_one()
            assert item.state == "failed"
            assert item.attempt_count == 1
            assert to_aware_utc(item.available_at_utc) > now
            item.available_at_utc = utc_now() - timedelta(seconds=1)

        await dispatch_notification_outbox_once(
            database.session,
            TransientFailureSender(),  # type: ignore[arg-type]
            _settings(),
        )
        async with database.session() as session:
            item = (await session.execute(select(NotificationOutbox))).scalar_one()
            assert item.state == "terminal_failed"
            assert item.attempt_count == 2
    finally:
        await database.dispose()


@pytest.mark.asyncio
async def test_bounded_retry_repository_respects_provider_delay(tmp_path) -> None:
    database = Database(f"sqlite+aiosqlite:///{tmp_path / 'bot.db'}")
    await database.initialize()
    await _add_users(database, [1001])
    now = utc_now()

    try:
        async with database.session() as session:
            user = (await session.execute(select(User))).scalar_one()
            item = NotificationOutbox(
                public_id="00000000-0000-0000-0000-000000001001",
                idempotency_key="retry-delay",
                user_id=user.id,
                notification_kind="admin_broadcast",
                payload_json=json.dumps({"message_text": "Текст"}),
                state="sending",
                attempt_count=1,
                available_at_utc=now,
                created_at_utc=now,
                updated_at_utc=now,
            )
            session.add(item)
            await session.flush()
            await NotificationOutboxRepository(session).mark_failed_bounded(
                item,
                "rate_limit",
                max_attempts=3,
                retry_base_seconds=30,
                retry_max_seconds=300,
                retry_after_seconds=120,
            )
            assert item.state == "failed"
            assert item.available_at_utc >= now + timedelta(seconds=119)
    finally:
        await database.dispose()
