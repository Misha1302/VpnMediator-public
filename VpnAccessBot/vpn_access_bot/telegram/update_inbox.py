from __future__ import annotations

import hashlib
from datetime import timedelta

from sqlalchemy import and_, or_, select, update
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.ext.asyncio import AsyncSession

from vpn_access_bot.logging_config import redact_text
from vpn_access_bot.models import TelegramUpdateInbox, utc_now

UPDATE_STATUS_PENDING = "pending"
UPDATE_STATUS_PROCESSING = "processing"
UPDATE_STATUS_RETRY = "retry"
UPDATE_STATUS_PROCESSED = "processed"
UPDATE_STATUS_QUARANTINED = "quarantined"
_REDACTED_PAYLOAD_JSON = '{"redacted":true}'


class TelegramUpdateInboxRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def receive(
        self,
        *,
        bot_key: str,
        update_id: int,
        payload_json: str,
    ) -> tuple[TelegramUpdateInbox, bool]:
        now = utc_now()
        payload_hash = hashlib.sha256(payload_json.encode("utf-8")).hexdigest()
        result = await self._session.execute(
            sqlite_insert(TelegramUpdateInbox)
            .values(
                bot_key=bot_key,
                update_id=update_id,
                payload_hash=payload_hash,
                payload_json=payload_json,
                status=UPDATE_STATUS_PENDING,
                attempt_count=0,
                received_at_utc=now,
                updated_at_utc=now,
            )
            .on_conflict_do_nothing(
                index_elements=[TelegramUpdateInbox.bot_key, TelegramUpdateInbox.update_id]
            )
        )
        inserted = result.rowcount == 1
        query = await self._session.execute(
            select(TelegramUpdateInbox).where(
                TelegramUpdateInbox.bot_key == bot_key,
                TelegramUpdateInbox.update_id == update_id,
            )
        )
        inbox = query.scalar_one()
        if not inserted and inbox.payload_hash != payload_hash:
            inbox.status = UPDATE_STATUS_QUARANTINED
            inbox.failure_code = "update_payload_conflict"
            inbox.last_error_message = (
                "Same bot/update identity was received with a different payload."
            )
            inbox.processed_at_utc = now
            inbox.claimed_by = None
            inbox.claim_expires_at_utc = None
            inbox.next_attempt_at_utc = None
            inbox.updated_at_utc = now
        return inbox, inserted

    async def claim_due(
        self,
        *,
        worker_id: str,
        limit: int,
        lease_seconds: int,
    ) -> list[TelegramUpdateInbox]:
        now = utc_now()
        lease_until = now + timedelta(seconds=max(lease_seconds, 1))
        claimable_status = or_(
            TelegramUpdateInbox.status.in_([UPDATE_STATUS_PENDING, UPDATE_STATUS_RETRY]),
            and_(
                TelegramUpdateInbox.status == UPDATE_STATUS_PROCESSING,
                TelegramUpdateInbox.claim_expires_at_utc.is_not(None),
                TelegramUpdateInbox.claim_expires_at_utc <= now,
            ),
        )
        result = await self._session.execute(
            select(TelegramUpdateInbox.id)
            .where(
                claimable_status,
                (
                    TelegramUpdateInbox.next_attempt_at_utc.is_(None)
                    | (TelegramUpdateInbox.next_attempt_at_utc <= now)
                ),
            )
            .order_by(TelegramUpdateInbox.received_at_utc, TelegramUpdateInbox.id)
            .limit(max(limit, 1))
        )
        claimed_ids: list[int] = []
        for inbox_id in result.scalars().all():
            claim = await self._session.execute(
                update(TelegramUpdateInbox)
                .where(
                    TelegramUpdateInbox.id == inbox_id,
                    or_(
                        TelegramUpdateInbox.status.in_(
                            [UPDATE_STATUS_PENDING, UPDATE_STATUS_RETRY]
                        ),
                        and_(
                            TelegramUpdateInbox.status == UPDATE_STATUS_PROCESSING,
                            TelegramUpdateInbox.claim_expires_at_utc.is_not(None),
                            TelegramUpdateInbox.claim_expires_at_utc <= now,
                        ),
                    ),
                )
                .values(
                    status=UPDATE_STATUS_PROCESSING,
                    claimed_by=worker_id,
                    claim_expires_at_utc=lease_until,
                    attempt_count=TelegramUpdateInbox.attempt_count + 1,
                    last_attempt_at_utc=now,
                    updated_at_utc=now,
                )
            )
            if claim.rowcount == 1:
                claimed_ids.append(int(inbox_id))

        if not claimed_ids:
            return []
        claimed = await self._session.execute(
            select(TelegramUpdateInbox)
            .where(TelegramUpdateInbox.id.in_(claimed_ids))
            .order_by(TelegramUpdateInbox.received_at_utc, TelegramUpdateInbox.id)
        )
        return list(claimed.scalars().all())

    async def mark_processed(self, inbox_id: int, *, worker_id: str) -> bool:
        now = utc_now()
        result = await self._session.execute(
            update(TelegramUpdateInbox)
            .where(
                TelegramUpdateInbox.id == inbox_id,
                TelegramUpdateInbox.status == UPDATE_STATUS_PROCESSING,
                TelegramUpdateInbox.claimed_by == worker_id,
            )
            .values(
                status=UPDATE_STATUS_PROCESSED,
                payload_json=_REDACTED_PAYLOAD_JSON,
                failure_code=None,
                last_error_message=None,
                claimed_by=None,
                claim_expires_at_utc=None,
                next_attempt_at_utc=None,
                processed_at_utc=now,
                updated_at_utc=now,
            )
        )
        return result.rowcount == 1

    async def mark_failed(
        self,
        inbox_id: int,
        *,
        worker_id: str,
        failure_code: str,
        error_message: str,
        max_attempts: int,
        retry_delay_seconds: int,
    ) -> str | None:
        query = await self._session.execute(
            select(TelegramUpdateInbox).where(
                TelegramUpdateInbox.id == inbox_id,
                TelegramUpdateInbox.status == UPDATE_STATUS_PROCESSING,
                TelegramUpdateInbox.claimed_by == worker_id,
            )
        )
        inbox = query.scalar_one_or_none()
        if inbox is None:
            return None

        now = utc_now()
        should_quarantine = inbox.attempt_count >= max(max_attempts, 1)
        inbox.status = UPDATE_STATUS_QUARANTINED if should_quarantine else UPDATE_STATUS_RETRY
        inbox.failure_code = failure_code[:64]
        inbox.last_error_message = redact_text(error_message)[:512]
        inbox.claimed_by = None
        inbox.claim_expires_at_utc = None
        inbox.next_attempt_at_utc = (
            None if should_quarantine else now + timedelta(seconds=max(retry_delay_seconds, 1))
        )
        inbox.processed_at_utc = now if should_quarantine else None
        inbox.updated_at_utc = now
        return inbox.status
