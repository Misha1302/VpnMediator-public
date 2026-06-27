from __future__ import annotations

import asyncio
import logging
from contextlib import suppress
from typing import Any
from uuid import uuid4

from aiogram import Bot, Dispatcher
from aiogram.exceptions import TelegramBadRequest
from aiogram.types import Update

from vpn_access_bot.config import Settings
from vpn_access_bot.db import Database
from vpn_access_bot.models import TelegramUpdateInbox
from vpn_access_bot.payment_processing import PaymentEvidence, PaymentInboxIngestionService
from vpn_access_bot.telegram.bot_registry import BotRegistry
from vpn_access_bot.telegram.update_inbox import (
    UPDATE_STATUS_QUARANTINED,
    TelegramUpdateInboxRepository,
)

logger = logging.getLogger(__name__)


_TERMINAL_QUERY_ERROR_MARKERS = (
    "query is too old",
    "query id is invalid",
)


def _is_terminal_query_error(exception: Exception) -> bool:
    if not isinstance(exception, TelegramBadRequest):
        return False

    message = str(exception).casefold()
    return any(marker in message for marker in _TERMINAL_QUERY_ERROR_MARKERS)


def _is_terminal_callback_error(update: Update, exception: Exception) -> bool:
    return update.callback_query is not None and _is_terminal_query_error(exception)


class ReliablePollingRunner:
    """Polls Telegram and persists non-checkout updates before acknowledging them."""

    def __init__(
        self,
        *,
        dispatcher: Dispatcher,
        registry: BotRegistry,
        database: Database,
        settings: Settings,
        workflow_data: dict[str, Any],
        polling_timeout_seconds: int = 10,
    ) -> None:
        self._dispatcher = dispatcher
        self._registry = registry
        self._database = database
        self._settings = settings
        self._workflow_data = dict(workflow_data)
        self._polling_timeout_seconds = max(polling_timeout_seconds, 1)
        self._update_semaphore = asyncio.Semaphore(self._settings.telegram_update_concurrency_limit)
        self._pending_updates: set[asyncio.Task[Any]] = set()

    async def run(self) -> None:
        bots = self._registry.bots
        if not bots:
            raise RuntimeError("No verified Telegram bots are available for polling.")

        workflow_data = {
            "dispatcher": self._dispatcher,
            "bots": tuple(bots),
            **self._dispatcher.workflow_data,
            **self._workflow_data,
        }
        workflow_data.pop("bot", None)
        allowed_updates = self._dispatcher.resolve_used_update_types()

        await self._dispatcher.emit_startup(bot=bots[-1], **workflow_data)
        tasks = [
            asyncio.create_task(
                self._poll_bot(bot, allowed_updates, workflow_data),
                name=f"telegram-polling:{self._registry.resolve(bot).key}",
            )
            for bot in bots
        ]
        tasks.append(
            asyncio.create_task(
                self._process_update_inbox_forever(workflow_data),
                name="telegram-update-inbox",
            )
        )
        try:
            await asyncio.gather(*tasks)
        finally:
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            await self._cancel_pending_updates()
            await self._dispatcher.emit_shutdown(bot=bots[-1], **workflow_data)

    async def _poll_bot(
        self,
        bot: Bot,
        allowed_updates: list[str],
        workflow_data: dict[str, Any],
    ) -> None:
        offset: int | None = None
        fetch_backoff_seconds = 1.0
        processing_backoff_seconds = 1.0
        while True:
            try:
                updates = await bot.get_updates(
                    offset=offset,
                    timeout=self._polling_timeout_seconds,
                    allowed_updates=allowed_updates,
                    request_timeout=self._polling_timeout_seconds + 15,
                )
                fetch_backoff_seconds = 1.0
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception(
                    "Failed to fetch Telegram updates: bot_key=%s",
                    self._registry.resolve(bot).key,
                )
                await asyncio.sleep(fetch_backoff_seconds)
                fetch_backoff_seconds = min(fetch_backoff_seconds * 2, 30.0)
                continue

            processing_failed = False
            for update in updates:
                try:
                    if self._is_successful_payment(update):
                        # If persistence fails, offset is intentionally not advanced. The next
                        # getUpdates call keeps the last safe offset, acknowledges earlier
                        # updates from the batch, and asks Telegram to redeliver this payment.
                        await self._durably_ingest_successful_payment(bot, update)

                    if update.pre_checkout_query is not None:
                        # Pre-checkout responses are time-sensitive and must not wait behind the
                        # ordinary update queue. A query that Telegram has already expired is a
                        # terminal transport outcome: retrying it can never succeed and must not
                        # hold the polling offset forever.
                        try:
                            await self._dispatcher.feed_update(bot, update, **workflow_data)
                        except Exception as exception:
                            if not _is_terminal_query_error(exception):
                                raise
                            logger.warning(
                                "Expired Telegram pre-checkout update discarded after terminal "
                                "API response: bot_key=%s update_id=%s error=%s",
                                self._registry.resolve(bot).key,
                                update.update_id,
                                type(exception).__name__,
                            )
                        offset = update.update_id + 1
                        continue

                    # Ordinary updates are acknowledged only after they have entered a durable,
                    # idempotent inbox. Handler failures are retried by the inbox worker and no
                    # longer turn an acknowledged Telegram update into an invisible loss.
                    await self._durably_ingest_update(bot, update)
                    offset = update.update_id + 1
                except asyncio.CancelledError:
                    raise
                except Exception:
                    logger.exception(
                        "Synchronous Telegram update processing failed; offset retained: "
                        "bot_key=%s update_id=%s offset=%s",
                        self._registry.resolve(bot).key,
                        update.update_id,
                        offset,
                    )
                    await asyncio.sleep(processing_backoff_seconds)
                    processing_backoff_seconds = min(processing_backoff_seconds * 2, 30.0)
                    processing_failed = True
                    break

            if not processing_failed:
                processing_backoff_seconds = 1.0

    async def _dispatch_with_semaphore(
        self,
        bot: Bot,
        update: Update,
        semaphore: asyncio.Semaphore,
        workflow_data: dict[str, Any],
    ) -> None:
        try:
            await self._dispatcher.feed_update(bot, update, **workflow_data)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception(
                "Telegram update handler failed: bot_key=%s update_id=%s",
                self._registry.resolve(bot).key,
                update.update_id,
            )
        finally:
            semaphore.release()

    async def _durably_ingest_update(self, bot: Bot, update: Update) -> None:
        bot_key = self._registry.resolve(bot).key
        payload_json = update.model_dump_json(exclude_none=True)
        async with self._database.session() as session:
            inbox, _ = await TelegramUpdateInboxRepository(session).receive(
                bot_key=bot_key,
                update_id=update.update_id,
                payload_json=payload_json,
            )
            if (
                inbox.status == UPDATE_STATUS_QUARANTINED
                and inbox.failure_code == "update_payload_conflict"
            ):
                raise RuntimeError(
                    "Telegram returned conflicting payloads for the same bot/update identity."
                )

    async def _process_update_inbox_forever(self, workflow_data: dict[str, Any]) -> None:
        worker_id = f"telegram-update:{uuid4().hex}"
        poll_interval = self._settings.telegram_update_inbox_poll_interval_seconds
        while True:
            try:
                async with self._database.session() as session:
                    claimed = await TelegramUpdateInboxRepository(session).claim_due(
                        worker_id=worker_id,
                        limit=self._settings.telegram_update_concurrency_limit,
                        lease_seconds=self._settings.telegram_update_lease_seconds,
                    )
                if not claimed:
                    await asyncio.sleep(poll_interval)
                    continue

                tasks = [
                    asyncio.create_task(
                        self._process_claimed_update(item, worker_id, workflow_data),
                        name=f"telegram-update:{item.bot_key}:{item.update_id}",
                    )
                    for item in claimed
                ]
                self._pending_updates.update(tasks)
                for task in tasks:
                    task.add_done_callback(self._pending_updates.discard)
                await asyncio.gather(*tasks)
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("Telegram update inbox worker iteration failed.")
                await asyncio.sleep(max(poll_interval, 1.0))

    async def _process_claimed_update(
        self,
        inbox: TelegramUpdateInbox,
        worker_id: str,
        workflow_data: dict[str, Any],
    ) -> None:
        runtime = self._registry.get(inbox.bot_key)
        try:
            if runtime is None or runtime.bot is None:
                raise RuntimeError("Telegram bot runtime is unavailable.")
            update = Update.model_validate_json(inbox.payload_json)
            await self._update_semaphore.acquire()
            try:
                await self._dispatcher.feed_update(runtime.bot, update, **workflow_data)
            finally:
                self._update_semaphore.release()
        except asyncio.CancelledError:
            raise
        except Exception as exception:
            if _is_terminal_callback_error(update, exception):
                async with self._database.session() as session:
                    marked = await TelegramUpdateInboxRepository(session).mark_processed(
                        inbox.id,
                        worker_id=worker_id,
                    )
                if marked:
                    logger.info(
                        "Discarded expired Telegram callback without retry: "
                        "bot_key=%s update_id=%s error=%s",
                        inbox.bot_key,
                        inbox.update_id,
                        type(exception).__name__,
                    )
                else:
                    logger.error(
                        "Non-retryable Telegram callback completed but its inbox lease was lost: "
                        "bot_key=%s update_id=%s",
                        inbox.bot_key,
                        inbox.update_id,
                    )
                return

            retry_delay = min(
                self._settings.telegram_update_retry_base_seconds
                * (2 ** max(inbox.attempt_count - 1, 0)),
                300,
            )
            async with self._database.session() as session:
                status = await TelegramUpdateInboxRepository(session).mark_failed(
                    inbox.id,
                    worker_id=worker_id,
                    failure_code=type(exception).__name__,
                    error_message=str(exception),
                    max_attempts=self._settings.telegram_update_retry_max_attempts,
                    retry_delay_seconds=retry_delay,
                )
            if status == UPDATE_STATUS_QUARANTINED:
                logger.error(
                    "Telegram update quarantined after repeated handler failures: "
                    "bot_key=%s update_id=%s attempts=%s",
                    inbox.bot_key,
                    inbox.update_id,
                    inbox.attempt_count,
                )
            else:
                logger.warning(
                    "Telegram update handler failed and will be retried: "
                    "bot_key=%s update_id=%s attempts=%s",
                    inbox.bot_key,
                    inbox.update_id,
                    inbox.attempt_count,
                    exc_info=True,
                )
            return

        async with self._database.session() as session:
            marked = await TelegramUpdateInboxRepository(session).mark_processed(
                inbox.id,
                worker_id=worker_id,
            )
        if not marked:
            logger.error(
                "Telegram update completed but its inbox lease was lost: bot_key=%s update_id=%s",
                inbox.bot_key,
                inbox.update_id,
            )

    async def _durably_ingest_successful_payment(self, bot: Bot, update: Update) -> None:
        message = update.message
        if message is None or message.successful_payment is None or message.from_user is None:
            raise RuntimeError("Successful payment update is missing required Telegram evidence.")

        payment = message.successful_payment
        bot_key = self._registry.resolve(bot).key
        async with self._database.session() as session:
            await PaymentInboxIngestionService(session).ingest_telegram_stars(
                PaymentEvidence(
                    invoice_payload=payment.invoice_payload,
                    amount_minor_units=payment.total_amount,
                    currency=payment.currency,
                    provider_charge_id=payment.telegram_payment_charge_id,
                    payer_telegram_id=message.from_user.id,
                    provider_occurred_at_utc=message.date,
                    payment_bot_key=bot_key,
                )
            )

    async def _cancel_pending_updates(self) -> None:
        tasks = list(self._pending_updates)
        for task in tasks:
            task.cancel()
        for task in tasks:
            with suppress(asyncio.CancelledError):
                await task
        self._pending_updates.clear()

    @staticmethod
    def _is_successful_payment(update: Update) -> bool:
        return update.message is not None and update.message.successful_payment is not None
