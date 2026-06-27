from __future__ import annotations

import hashlib
import json
import re
import secrets
from collections.abc import Callable
from contextlib import AbstractAsyncContextManager
from dataclasses import dataclass
from datetime import datetime, timedelta
from uuid import uuid4

from sqlalchemy import func, select, update
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.ext.asyncio import AsyncSession

from vpn_access_bot.models import (
    BroadcastCampaign,
    BroadcastRecipient,
    NotificationOutbox,
    User,
    utc_now,
)
from vpn_access_bot.repositories import (
    AuditRepository,
    NotificationOutboxRepository,
    to_aware_utc,
)
from vpn_access_bot.telegram.context import get_bot_key

BROADCAST_MAX_MESSAGE_LENGTH = 4096
BROADCAST_MAX_PATTERN_LENGTH = 128
BROADCAST_CONFIRMATION_TOKEN_MIN_LENGTH = 16
BROADCAST_CONFIRMATION_TOKEN_MAX_LENGTH = 64
BROADCAST_NOTIFICATION_KIND = "admin_broadcast"
BROADCAST_PREPARE_BATCH_SIZE = 250
BROADCAST_QUEUE_BATCH_SIZE = 250

SessionFactory = Callable[[], AbstractAsyncContextManager[AsyncSession]]


class BroadcastCommandError(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class BroadcastRequest:
    message_text: str
    pattern: str | None = None


@dataclass(frozen=True, slots=True)
class BroadcastPreview:
    campaign_id: str
    confirmation_token: str
    target_count: int
    message_length: int
    message_sha256: str
    expires_at_utc: datetime
    pattern: str | None


@dataclass(frozen=True, slots=True)
class BroadcastEnqueueResult:
    target_count: int
    newly_queued_count: int
    already_queued_count: int
    campaign_id: str


@dataclass(frozen=True, slots=True)
class BroadcastTarget:
    user_id: int
    telegram_id: int


def parse_broadcast_command(raw_text: str | None, *, filtered: bool) -> BroadcastRequest:
    normalized = (raw_text or "").replace("\r\n", "\n").replace("\r", "\n")
    header, separator, body = normalized.partition("\n")
    tokens = header.split()

    if filtered:
        if len(tokens) != 2:
            raise BroadcastCommandError("invalid_command")
        pattern = tokens[1]
        _validate_pattern(pattern)
    else:
        if len(tokens) != 1:
            raise BroadcastCommandError("invalid_command")
        pattern = None

    if not separator:
        raise BroadcastCommandError("message_required")

    message_text = body.strip()
    if not message_text:
        raise BroadcastCommandError("message_required")
    if "\x00" in message_text:
        raise BroadcastCommandError("message_contains_nul")
    if len(message_text) > BROADCAST_MAX_MESSAGE_LENGTH:
        raise BroadcastCommandError("message_too_long")

    return BroadcastRequest(message_text=message_text, pattern=pattern)


def parse_broadcast_confirmation(raw_text: str | None) -> str:
    tokens = (raw_text or "").split()
    if len(tokens) != 2:
        raise BroadcastCommandError("confirmation_token_required")
    token = tokens[1]
    if not (
        BROADCAST_CONFIRMATION_TOKEN_MIN_LENGTH
        <= len(token)
        <= BROADCAST_CONFIRMATION_TOKEN_MAX_LENGTH
    ):
        raise BroadcastCommandError("confirmation_token_invalid")
    if re.fullmatch(r"[A-Za-z0-9_-]+", token) is None:
        raise BroadcastCommandError("confirmation_token_invalid")
    return token


def compile_recipient_pattern(pattern: str) -> re.Pattern[str]:
    _validate_pattern(pattern)
    return re.compile(pattern)


def _validate_pattern(pattern: str) -> None:
    if not pattern or len(pattern) > BROADCAST_MAX_PATTERN_LENGTH:
        raise BroadcastCommandError("invalid_regex")
    if any(character.isspace() for character in pattern):
        raise BroadcastCommandError("invalid_regex")

    # Telegram IDs are short decimal strings. Keep the accepted regex language
    # deliberately bounded so an accidental catastrophic-backtracking pattern
    # cannot hold the bot worker while iterating over the user table.
    if "(?" in pattern or re.search(r"\\[1-9]", pattern) is not None:
        raise BroadcastCommandError("unsafe_regex")
    if re.search(r"\)(?:[*+?]|\{\d+(?:,\d*)?\})", pattern) is not None:
        raise BroadcastCommandError("unsafe_regex")
    if (
        re.search(
            r"(?:[*+?]|\{\d+(?:,\d*)?\})(?:[*+?]|\{)",
            pattern,
        )
        is not None
    ):
        raise BroadcastCommandError("unsafe_regex")

    try:
        re.compile(pattern)
    except re.error as exception:
        raise BroadcastCommandError("invalid_regex") from exception


def _token_hash(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


class BroadcastService:
    def __init__(self, session_factory: SessionFactory) -> None:
        self._session_factory = session_factory

    async def prepare(
        self,
        request: BroadcastRequest,
        *,
        admin_telegram_id: int,
        source_chat_id: int,
        source_message_id: int,
        confirmation_ttl_minutes: int,
    ) -> BroadcastPreview | None:
        now = utc_now()
        confirmation_ttl = timedelta(minutes=max(confirmation_ttl_minutes, 1))
        # The confirmation window starts after the recipient snapshot is ready.
        # A generous preparation deadline prevents a retention run from expiring
        # an in-progress snapshot for a large user table.
        expires_at = now + max(confirmation_ttl, timedelta(hours=1))
        source_bot_key = get_bot_key() or "default"
        token = secrets.token_urlsafe(18)
        token_hash = _token_hash(token)
        message_hash = hashlib.sha256(request.message_text.encode("utf-8")).hexdigest()
        campaign_public_id = str(uuid4())

        async with self._session_factory() as session:
            upper_bound = int(
                (await session.scalar(select(func.coalesce(func.max(User.id), 0)))) or 0
            )
            insert_result = await session.execute(
                sqlite_insert(BroadcastCampaign)
                .values(
                    public_id=campaign_public_id,
                    admin_telegram_id=admin_telegram_id,
                    source_bot_key=source_bot_key,
                    source_chat_id=source_chat_id,
                    source_message_id=source_message_id,
                    filter_kind="regex" if request.pattern is not None else "all",
                    recipient_pattern=request.pattern,
                    recipient_upper_bound_user_id=upper_bound,
                    message_text=request.message_text,
                    message_sha256=message_hash,
                    confirmation_token_hash=token_hash,
                    state="preparing",
                    target_count=0,
                    queued_count=0,
                    expires_at_utc=expires_at,
                    created_at_utc=now,
                    updated_at_utc=now,
                )
                .on_conflict_do_nothing(
                    index_elements=[
                        BroadcastCampaign.source_bot_key,
                        BroadcastCampaign.source_chat_id,
                        BroadcastCampaign.source_message_id,
                    ]
                )
                .returning(BroadcastCampaign.id)
            )
            campaign_id = insert_result.scalar_one_or_none()
            if campaign_id is None:
                existing = (
                    await session.execute(
                        select(BroadcastCampaign).where(
                            BroadcastCampaign.source_bot_key == source_bot_key,
                            BroadcastCampaign.source_chat_id == source_chat_id,
                            BroadcastCampaign.source_message_id == source_message_id,
                        )
                    )
                ).scalar_one_or_none()
                if existing is None:
                    raise RuntimeError("broadcast_campaign_conflict_without_row")
                if (
                    existing.admin_telegram_id != admin_telegram_id
                    or existing.filter_kind != ("regex" if request.pattern is not None else "all")
                    or existing.recipient_pattern != request.pattern
                    or existing.message_sha256 != message_hash
                ):
                    raise BroadcastCommandError("campaign_already_exists")
                if existing.state == "empty":
                    return None
                if existing.state == "awaiting_confirmation":
                    existing.confirmation_token_hash = token_hash
                    existing.expires_at_utc = expires_at
                    existing.updated_at_utc = now
                    await AuditRepository(session).add(
                        event_type="admin.broadcast.confirmation_rotated",
                        telegram_id=admin_telegram_id,
                        details_json=json.dumps(
                            {
                                "campaign_id": existing.public_id,
                                "target_count": existing.target_count,
                                "message_sha256": existing.message_sha256,
                                "expires_at_utc": expires_at.isoformat(),
                            },
                            sort_keys=True,
                        ),
                    )
                    return BroadcastPreview(
                        campaign_id=existing.public_id,
                        confirmation_token=token,
                        target_count=existing.target_count,
                        message_length=len(existing.message_text),
                        message_sha256=existing.message_sha256,
                        expires_at_utc=expires_at,
                        pattern=existing.recipient_pattern,
                    )
                if existing.state in {"enqueuing", "queued"}:
                    raise BroadcastCommandError("campaign_already_queued")
                raise BroadcastCommandError("campaign_already_exists")

        compiled = (
            compile_recipient_pattern(request.pattern) if request.pattern is not None else None
        )
        target_count = 0
        last_user_id = 0
        try:
            while last_user_id < upper_bound:
                async with self._session_factory() as session:
                    rows = list(
                        (
                            await session.execute(
                                select(User.id, User.telegram_id)
                                .where(
                                    User.id > last_user_id,
                                    User.id <= upper_bound,
                                )
                                .order_by(User.id)
                                .limit(BROADCAST_PREPARE_BATCH_SIZE)
                            )
                        ).all()
                    )
                    if not rows:
                        break
                    last_user_id = int(rows[-1][0])
                    targets = [
                        BroadcastTarget(user_id=int(user_id), telegram_id=int(telegram_id))
                        for user_id, telegram_id in rows
                        if compiled is None or compiled.fullmatch(str(int(telegram_id))) is not None
                    ]
                    if targets:
                        created_at = utc_now()
                        inserted = await session.execute(
                            sqlite_insert(BroadcastRecipient)
                            .values(
                                [
                                    {
                                        "campaign_id": int(campaign_id),
                                        "user_id": target.user_id,
                                        "telegram_id": target.telegram_id,
                                        "created_at_utc": created_at,
                                    }
                                    for target in targets
                                ]
                            )
                            .on_conflict_do_nothing(
                                index_elements=[
                                    BroadcastRecipient.campaign_id,
                                    BroadcastRecipient.user_id,
                                ]
                            )
                            .returning(BroadcastRecipient.user_id)
                        )
                        target_count += len(list(inserted.scalars()))

            async with self._session_factory() as session:
                campaign = await session.get(BroadcastCampaign, int(campaign_id))
                if campaign is None:
                    raise RuntimeError("broadcast_campaign_missing")
                confirmation_expires_at = utc_now() + confirmation_ttl
                campaign.target_count = target_count
                campaign.state = "awaiting_confirmation" if target_count > 0 else "empty"
                campaign.expires_at_utc = confirmation_expires_at
                campaign.updated_at_utc = utc_now()
                expires_at = confirmation_expires_at
                await AuditRepository(session).add(
                    event_type="admin.broadcast.prepared",
                    telegram_id=admin_telegram_id,
                    details_json=json.dumps(
                        {
                            "campaign_id": campaign_public_id,
                            "filter_kind": campaign.filter_kind,
                            "pattern": request.pattern,
                            "target_count": target_count,
                            "message_length": len(request.message_text),
                            "message_sha256": message_hash,
                            "expires_at_utc": expires_at.isoformat(),
                        },
                        ensure_ascii=False,
                        sort_keys=True,
                    ),
                )
        except Exception:
            async with self._session_factory() as session:
                campaign = await session.get(BroadcastCampaign, int(campaign_id))
                if campaign is not None and campaign.state == "preparing":
                    campaign.state = "failed"
                    campaign.updated_at_utc = utc_now()
            raise

        if target_count == 0:
            return None
        return BroadcastPreview(
            campaign_id=campaign_public_id,
            confirmation_token=token,
            target_count=target_count,
            message_length=len(request.message_text),
            message_sha256=message_hash,
            expires_at_utc=expires_at,
            pattern=request.pattern,
        )

    async def confirm_and_enqueue(
        self,
        confirmation_token: str,
        *,
        admin_telegram_id: int,
        source_chat_id: int,
    ) -> BroadcastEnqueueResult:
        token_hash = _token_hash(confirmation_token)
        source_bot_key = get_bot_key() or "default"
        transitioned = False

        async with self._session_factory() as session:
            campaign = (
                await session.execute(
                    select(BroadcastCampaign).where(
                        BroadcastCampaign.confirmation_token_hash == token_hash,
                        BroadcastCampaign.admin_telegram_id == admin_telegram_id,
                        BroadcastCampaign.source_bot_key == source_bot_key,
                        BroadcastCampaign.source_chat_id == source_chat_id,
                    )
                )
            ).scalar_one_or_none()
            if campaign is None:
                raise BroadcastCommandError("confirmation_token_invalid")

            now = utc_now()
            if (
                campaign.state == "awaiting_confirmation"
                and to_aware_utc(campaign.expires_at_utc) <= now
            ):
                campaign.state = "expired"
                campaign.updated_at_utc = now
                await AuditRepository(session).add(
                    event_type="admin.broadcast.expired",
                    telegram_id=admin_telegram_id,
                    details_json=json.dumps({"campaign_id": campaign.public_id}, sort_keys=True),
                )
                await session.commit()
                raise BroadcastCommandError("confirmation_token_expired")
            if campaign.state == "empty":
                raise BroadcastCommandError("campaign_has_no_recipients")
            if campaign.state == "preparing":
                raise BroadcastCommandError("campaign_still_preparing")
            if campaign.state in {"expired", "failed"}:
                raise BroadcastCommandError("campaign_not_confirmable")
            if campaign.state == "queued":
                return BroadcastEnqueueResult(
                    target_count=campaign.target_count,
                    newly_queued_count=0,
                    already_queued_count=campaign.queued_count,
                    campaign_id=campaign.public_id,
                )

            if campaign.state == "awaiting_confirmation":
                result = await session.execute(
                    update(BroadcastCampaign)
                    .where(
                        BroadcastCampaign.id == campaign.id,
                        BroadcastCampaign.state == "awaiting_confirmation",
                    )
                    .values(
                        state="enqueuing",
                        confirmed_at_utc=now,
                        updated_at_utc=now,
                    )
                )
                transitioned = bool(result.rowcount)
                if transitioned:
                    await AuditRepository(session).add(
                        event_type="admin.broadcast.confirmed",
                        telegram_id=admin_telegram_id,
                        details_json=json.dumps(
                            {
                                "campaign_id": campaign.public_id,
                                "target_count": campaign.target_count,
                            },
                            sort_keys=True,
                        ),
                    )
            campaign_id = campaign.id
            campaign_public_id = campaign.public_id
            campaign_bot_key = campaign.source_bot_key
            target_count = campaign.target_count

        newly_queued = 0
        last_user_id = 0
        while True:
            async with self._session_factory() as session:
                recipient_rows = list(
                    (
                        await session.execute(
                            select(BroadcastRecipient.user_id)
                            .where(
                                BroadcastRecipient.campaign_id == campaign_id,
                                BroadcastRecipient.user_id > last_user_id,
                            )
                            .order_by(BroadcastRecipient.user_id)
                            .limit(BROADCAST_QUEUE_BATCH_SIZE)
                        )
                    ).scalars()
                )
                if not recipient_rows:
                    break
                user_ids = [int(user_id) for user_id in recipient_rows]
                last_user_id = user_ids[-1]
                newly_queued += await NotificationOutboxRepository(session).bulk_enqueue_broadcast(
                    campaign_id=campaign_id,
                    campaign_public_id=campaign_public_id,
                    bot_key=campaign_bot_key,
                    user_ids=user_ids,
                )

        async with self._session_factory() as session:
            queued_count = int(
                (
                    await session.scalar(
                        select(func.count(NotificationOutbox.id)).where(
                            NotificationOutbox.broadcast_campaign_id == campaign_id,
                            NotificationOutbox.notification_kind == BROADCAST_NOTIFICATION_KIND,
                        )
                    )
                )
                or 0
            )
            campaign = await session.get(BroadcastCampaign, campaign_id)
            if campaign is None:
                raise RuntimeError("broadcast_campaign_missing")
            if queued_count != target_count:
                campaign.updated_at_utc = utc_now()
                raise RuntimeError("broadcast_campaign_queue_incomplete")
            was_queued = campaign.state == "queued"
            campaign.state = "queued"
            campaign.queued_count = queued_count
            campaign.queued_at_utc = campaign.queued_at_utc or utc_now()
            campaign.updated_at_utc = utc_now()
            if not was_queued:
                await AuditRepository(session).add(
                    event_type="admin.broadcast.enqueued",
                    telegram_id=admin_telegram_id,
                    details_json=json.dumps(
                        {
                            "campaign_id": campaign.public_id,
                            "target_count": target_count,
                            "newly_queued_count": newly_queued,
                            "already_queued_count": target_count - newly_queued,
                            "confirmation_transitioned": transitioned,
                        },
                        sort_keys=True,
                    ),
                )

        return BroadcastEnqueueResult(
            target_count=target_count,
            newly_queued_count=newly_queued,
            already_queued_count=target_count - newly_queued,
            campaign_id=campaign_public_id,
        )
