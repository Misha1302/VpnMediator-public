from __future__ import annotations

from dataclasses import dataclass, field
from types import SimpleNamespace
from typing import Any, ClassVar

import pytest

from vpn_access_bot.config import Settings
from vpn_access_bot.handlers import admin


@dataclass
class FakeMessage:
    text: str
    answers: list[str] = field(default_factory=list)
    message_id: int = 10
    from_user: Any = field(default_factory=lambda: SimpleNamespace(id=11))
    chat: Any = field(default_factory=lambda: SimpleNamespace(id=-100))

    async def answer(self, text: str, **_: object) -> None:
        self.answers.append(text)


class FakeSessionContext:
    async def __aenter__(self) -> object:
        return object()

    async def __aexit__(self, *_: object) -> None:
        return None


class FakeDatabase:
    def session(self) -> FakeSessionContext:
        return FakeSessionContext()


class RejectingRepairService:
    def __init__(self, _session: object, _mediator_client: object) -> None:
        pass

    async def apply(self, **_: object) -> object:
        raise ValueError("secret_path=/etc/vpn-mediator/mediator.env")


@pytest.mark.asyncio
async def test_reconciliation_handler_never_exposes_unknown_exception_text(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(admin, "ReconciliationRepairService", RejectingRepairService)
    message = FakeMessage("/reconcile_adopt_remote guid reason")
    settings = Settings(
        TELEGRAM_BOT_TOKEN="test-token",
        MEDIATOR_ADMIN_TOKEN="test-admin-token",
        ADMIN_TELEGRAM_IDS="11",
    )

    await admin._handle_reconciliation_repair(
        message,
        FakeDatabase(),
        settings,
        object(),
        mode="adopt_remote",
    )

    assert message.answers == [
        "Операция отклонена проверками безопасности. Повторите /reconcile_status."
    ]
    assert "secret_path" not in message.answers[0]
    assert "/etc/vpn-mediator" not in message.answers[0]


def test_known_reconciliation_error_has_safe_operator_message() -> None:
    code, text = admin._safe_reconciliation_rejection_text(
        ValueError("reconciliation_snapshot_changed")
    )

    assert code == "reconciliation_snapshot_changed"
    assert "/reconcile_status" in text


class CapturingRepairService:
    calls: ClassVar[list[dict[str, object]]] = []

    def __init__(self, _session: object, _mediator_client: object) -> None:
        pass

    async def apply(self, **kwargs: object) -> object:
        self.calls.append(kwargs)
        return SimpleNamespace(
            mode="adopt_disabled",
            subscription=SimpleNamespace(
                public_guid=kwargs["public_guid"],
                status="disabled",
            ),
            status="disabled",
            version=kwargs["expected_remote_version"],
        )


@pytest.mark.asyncio
async def test_adopt_disabled_handler_passes_explicit_snapshot_version_and_reason(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    CapturingRepairService.calls = []
    monkeypatch.setattr(admin, "ReconciliationRepairService", CapturingRepairService)
    public_guid = "00000000-0000-0000-0000-000000000001"
    message = FakeMessage(
        f"/reconcile_adopt_disabled {public_guid} 7 confirmed administrative revoke"
    )
    settings = Settings(
        TELEGRAM_BOT_TOKEN="test-token",
        MEDIATOR_ADMIN_TOKEN="test-admin-token",
        ADMIN_TELEGRAM_IDS="11",
    )

    await admin._handle_reconciliation_repair(
        message,
        FakeDatabase(),
        settings,
        object(),
        mode="adopt_disabled",
    )

    assert CapturingRepairService.calls == [
        {
            "public_guid": public_guid,
            "actor_telegram_id": 11,
            "source_request_id": "telegram:-100:10",
            "reason": "confirmed administrative revoke",
            "mode": "adopt_disabled",
            "expected_remote_version": 7,
        }
    ]
    assert len(message.answers) == 1
    assert "принудительное отключение" in message.answers[0]
    assert "version <b>7</b>" in message.answers[0]
