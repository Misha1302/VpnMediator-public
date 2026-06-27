from __future__ import annotations

from datetime import UTC, datetime

from aiogram.enums import ChatType
from aiogram.types import CallbackQuery, Chat, Message, PreCheckoutQuery, Update, User

from vpn_access_bot.access_boundary import evaluate_update_boundary
from vpn_access_bot.config import Settings


def _settings() -> Settings:
    return Settings(
        TELEGRAM_BOT_TOKEN="test-token",
        MEDIATOR_ADMIN_TOKEN="test-admin-token",
        ADMIN_TELEGRAM_IDS="11",
        SUPPORT_AGENT_TELEGRAM_IDS="22",
        SUPPORT_CHAT_ID=-100200,
    )


def _message(*, chat_type: ChatType, chat_id: int, actor_id: int, text: str) -> Message:
    return Message(
        message_id=1,
        date=datetime.now(UTC),
        chat=Chat(id=chat_id, type=chat_type),
        from_user=User(id=actor_id, is_bot=False, first_name="Actor"),
        text=text,
    )


def test_private_customer_message_is_allowed() -> None:
    update = Update(
        update_id=1,
        message=_message(
            chat_type=ChatType.PRIVATE,
            chat_id=123,
            actor_id=123,
            text="/start",
        ),
    )

    assert evaluate_update_boundary(update, _settings()).allowed is True


def test_group_customer_command_is_blocked_with_notice() -> None:
    update = Update(
        update_id=2,
        message=_message(
            chat_type=ChatType.GROUP,
            chat_id=-100,
            actor_id=123,
            text="/start",
        ),
    )

    decision = evaluate_update_boundary(update, _settings())

    assert decision.allowed is False
    assert decision.notify_user is True


def test_group_customer_callback_is_blocked() -> None:
    message = _message(
        chat_type=ChatType.SUPERGROUP,
        chat_id=-100,
        actor_id=123,
        text="menu",
    )
    update = Update(
        update_id=3,
        callback_query=CallbackQuery(
            id="callback",
            from_user=User(id=123, is_bot=False, first_name="Actor"),
            chat_instance="instance",
            message=message,
            data="buy:start",
        ),
    )

    decision = evaluate_update_boundary(update, _settings())

    assert decision.allowed is False
    assert decision.notify_user is True


def test_allowlisted_admin_command_is_allowed_in_group() -> None:
    update = Update(
        update_id=4,
        message=_message(
            chat_type=ChatType.SUPERGROUP,
            chat_id=-100,
            actor_id=11,
            text="/pending_orders",
        ),
    )

    assert evaluate_update_boundary(update, _settings()).allowed is True


def test_admin_customer_command_remains_blocked_in_group() -> None:
    update = Update(
        update_id=5,
        message=_message(
            chat_type=ChatType.SUPERGROUP,
            chat_id=-100,
            actor_id=11,
            text="/start",
        ),
    )

    assert evaluate_update_boundary(update, _settings()).allowed is False


def test_support_reply_requires_allowlisted_agent_and_support_chat() -> None:
    root = _message(
        chat_type=ChatType.SUPERGROUP,
        chat_id=-100200,
        actor_id=999,
        text="ticket",
    )
    allowed = _message(
        chat_type=ChatType.SUPERGROUP,
        chat_id=-100200,
        actor_id=22,
        text="reply",
    ).model_copy(update={"reply_to_message": root})
    denied = allowed.model_copy(update={"from_user": User(id=23, is_bot=False, first_name="Other")})

    assert evaluate_update_boundary(Update(update_id=6, message=allowed), _settings()).allowed
    assert not evaluate_update_boundary(Update(update_id=7, message=denied), _settings()).allowed


def test_pre_checkout_is_deferred_to_owner_scoped_commerce_service() -> None:
    update = Update(
        update_id=8,
        pre_checkout_query=PreCheckoutQuery(
            id="pre-checkout",
            from_user=User(id=123, is_bot=False, first_name="Payer"),
            currency="XTR",
            total_amount=60,
            invoice_payload="order:test",
        ),
    )

    assert evaluate_update_boundary(update, _settings()).allowed is True


def test_reconciliation_commands_are_admin_only_group_operations() -> None:
    settings = _settings()

    for index, command in enumerate(
        (
            "/reconcile_status guid",
            "/reconcile_adopt_remote guid reason",
            "/reconcile_adopt_expired guid 2 reason",
            "/reconcile_adopt_disabled guid 2 reason",
            "/reconcile_restore_local guid reason",
        ),
        start=20,
    ):
        admin_update = Update(
            update_id=index,
            message=_message(
                chat_type=ChatType.SUPERGROUP,
                chat_id=-100,
                actor_id=11,
                text=command,
            ),
        )
        customer_update = Update(
            update_id=index + 100,
            message=_message(
                chat_type=ChatType.SUPERGROUP,
                chat_id=-100,
                actor_id=123,
                text=command,
            ),
        )

        assert evaluate_update_boundary(admin_update, settings).allowed is True
        assert evaluate_update_boundary(customer_update, settings).allowed is False


def test_broadcast_commands_are_private_even_for_admins() -> None:
    settings = _settings()

    for index, command in enumerate(
        (
            "/broadcast\nПервая строка\nВторая строка",
            "/broadcast_regex ^123$\nСообщение",
            "/broadcast_confirm abcdefghijklmnop",
        ),
        start=40,
    ):
        admin_update = Update(
            update_id=index,
            message=_message(
                chat_type=ChatType.SUPERGROUP,
                chat_id=-100,
                actor_id=11,
                text=command,
            ),
        )
        customer_update = Update(
            update_id=index + 100,
            message=_message(
                chat_type=ChatType.SUPERGROUP,
                chat_id=-100,
                actor_id=123,
                text=command,
            ),
        )

        admin_decision = evaluate_update_boundary(admin_update, settings)
        assert admin_decision.allowed is False
        assert admin_decision.notify_user is True
        assert evaluate_update_boundary(customer_update, settings).allowed is False
