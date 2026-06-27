from __future__ import annotations

from dataclasses import dataclass, field
from datetime import timedelta
from types import SimpleNamespace
from typing import Any, ClassVar

import pytest

from vpn_access_bot.config import Settings
from vpn_access_bot.handlers import admin
from vpn_access_bot.models import utc_now


@dataclass
class FakeMessage:
    text: str
    sender_id: int
    answers: list[str] = field(default_factory=list)
    message_id: int = 44
    chat_id: int = -100123

    @property
    def from_user(self) -> Any:
        return SimpleNamespace(id=self.sender_id)

    @property
    def chat(self) -> Any:
        return SimpleNamespace(id=self.chat_id)

    async def answer(self, text: str, **_: object) -> None:
        self.answers.append(text)


class ForbiddenDatabase:
    def session(self) -> object:
        raise AssertionError("Unauthorized handler opened a database session.")


class FakeSessionContext:
    async def __aenter__(self) -> object:
        return object()

    async def __aexit__(self, *_: object) -> None:
        return None


class FakeDatabase:
    def session(self) -> FakeSessionContext:
        return FakeSessionContext()


class CapturingAdjustmentService:
    calls: ClassVar[list[dict[str, object]]] = []

    def __init__(self, _session: object, _mediator_client: object) -> None:
        pass

    async def apply(self, **kwargs: object) -> object:
        self.calls.append(kwargs)
        return SimpleNamespace(
            operation_public_id="operation-1",
            subscription=SimpleNamespace(
                public_guid=kwargs["public_guid"],
                expires_at=utc_now() + timedelta(days=30),
                max_devices=6,
            ),
            version=2,
            status="active",
        )


def _settings() -> Settings:
    return Settings(
        TELEGRAM_BOT_TOKEN="test-token",
        MEDIATOR_ADMIN_TOKEN="test-admin-token-with-sufficient-length",
        ADMIN_TELEGRAM_IDS="11",
    )


@pytest.mark.asyncio
async def test_admin_router_filter_rejects_non_admin_and_accepts_admin() -> None:
    filter_ = admin.AdminMessageFilter()

    assert not await filter_(FakeMessage("/admin", sender_id=12), _settings())
    assert await filter_(FakeMessage("/admin", sender_id=11), _settings())


@pytest.mark.asyncio
async def test_non_admin_cannot_adjust_subscription() -> None:
    message = FakeMessage(
        "/adjust_subscription 00000000-0000-0000-0000-000000000001 30 6 forged",
        sender_id=12,
    )

    await admin.handle_adjust_subscription(
        message,
        ForbiddenDatabase(),
        _settings(),
        object(),
    )

    assert message.answers == []


@pytest.mark.asyncio
async def test_non_admin_cannot_revoke_subscription() -> None:
    message = FakeMessage(
        "/revoke_subscription 00000000-0000-0000-0000-000000000001 forged",
        sender_id=12,
    )

    await admin.handle_revoke_subscription(
        message,
        ForbiddenDatabase(),
        _settings(),
        object(),
    )

    assert message.answers == []


@pytest.mark.asyncio
async def test_adjustment_uses_authenticated_admin_identity(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    CapturingAdjustmentService.calls = []
    monkeypatch.setattr(
        admin,
        "AdminEntitlementAdjustmentService",
        CapturingAdjustmentService,
    )
    public_guid = "00000000-0000-0000-0000-000000000001"
    message = FakeMessage(
        f"/adjust_subscription {public_guid} 30 6 support compensation",
        sender_id=11,
    )

    await admin.handle_adjust_subscription(
        message,
        FakeDatabase(),
        _settings(),
        object(),
    )

    assert CapturingAdjustmentService.calls == [
        {
            "public_guid": public_guid,
            "actor_telegram_id": 11,
            "source_request_id": "telegram:-100123:44",
            "reason": "support compensation",
            "duration_days": 30,
            "requested_device_limit": 6,
        }
    ]
    assert len(message.answers) == 1
    assert "Изменение применено" in message.answers[0]
