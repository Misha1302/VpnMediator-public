from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass

from aiogram import BaseMiddleware
from aiogram.enums import ChatType
from aiogram.types import CallbackQuery, Message, TelegramObject, Update

from vpn_access_bot.config import Settings

PRIVATE_CHAT_REQUIRED_TEXT = "Откройте бота в личном чате, чтобы управлять подпиской и оплатой."

_ADMIN_COMMANDS = frozenset(
    {
        "admin",
        "broadcast",
        "broadcast_confirm",
        "broadcast_regex",
        "pending_orders",
        "failed_orders",
        "order",
        "approve_order",
        "retry_order",
        "refund_order",
        "sync_expired",
        "reconcile_status",
        "reconcile_adopt_remote",
        "reconcile_adopt_expired",
        "reconcile_adopt_disabled",
        "reconcile_restore_local",
        "test_buy",
        "referral_stats",
        "discount_set",
        "discount_show",
        "discount_list",
        "discount_remove",
        "support_close",
        "workers",
        "commerce_status",
        "commerce_stop",
        "commerce_start",
        "capacity_status",
        "campaign_create",
        "campaign_status",
        "confirm_refund",
        "product_funnel",
    }
)

_PRIVATE_ADMIN_COMMANDS = frozenset(
    {
        "broadcast",
        "broadcast_confirm",
        "broadcast_regex",
    }
)


@dataclass(frozen=True, slots=True)
class BoundaryDecision:
    allowed: bool
    notify_user: bool = False


def _command_name(message: Message) -> str | None:
    text = message.text or message.caption

    if not text or not text.startswith("/"):
        return None

    first_token = text.split(maxsplit=1)[0]
    command = first_token[1:].split("@", maxsplit=1)[0].strip().lower()
    return command or None


def evaluate_update_boundary(update: Update, settings: Settings) -> BoundaryDecision:
    pre_checkout = update.pre_checkout_query

    if pre_checkout is not None:
        # Pre-checkout queries do not carry a chat. Payer authorization belongs to
        # the commerce service and is checked against pre_checkout.from_user.
        return BoundaryDecision(allowed=True)

    callback = update.callback_query

    if callback is not None:
        message = callback.message

        if message is not None and message.chat.type == ChatType.PRIVATE:
            return BoundaryDecision(allowed=True)

        return BoundaryDecision(allowed=False, notify_user=True)

    message = update.message or update.edited_message

    if message is None:
        return BoundaryDecision(allowed=True)

    if message.chat.type == ChatType.PRIVATE:
        return BoundaryDecision(allowed=True)

    actor = message.from_user

    if actor is None:
        return BoundaryDecision(allowed=False)

    if (
        settings.support_chat_id is not None
        and message.chat.id == settings.support_chat_id
        and actor.id in settings.support_agent_telegram_ids
        and message.reply_to_message is not None
    ):
        return BoundaryDecision(allowed=True)

    command = _command_name(message)

    if command in _PRIVATE_ADMIN_COMMANDS:
        return BoundaryDecision(allowed=False, notify_user=True)

    if actor.id in settings.admin_telegram_ids and command in _ADMIN_COMMANDS:
        return BoundaryDecision(allowed=True)

    return BoundaryDecision(
        allowed=False,
        notify_user=command is not None,
    )


class PrivateCustomerBoundaryMiddleware(BaseMiddleware):
    def __init__(self, settings: Settings) -> None:
        self._settings = settings

    async def __call__(
        self,
        handler: Callable[[TelegramObject, dict], Awaitable[object]],
        event: TelegramObject,
        data: dict,
    ) -> object | None:
        if not isinstance(event, Update):
            return await handler(event, data)

        decision = evaluate_update_boundary(event, self._settings)

        if decision.allowed:
            return await handler(event, data)

        await _notify_private_chat_required(event, decision.notify_user)
        return None


async def _notify_private_chat_required(update: Update, notify_user: bool) -> None:
    if not notify_user:
        return

    callback: CallbackQuery | None = update.callback_query

    if callback is not None:
        await callback.answer(PRIVATE_CHAT_REQUIRED_TEXT, show_alert=True)
        return

    message: Message | None = update.message or update.edited_message

    if message is not None:
        await message.answer(PRIVATE_CHAT_REQUIRED_TEXT)
