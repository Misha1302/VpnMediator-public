from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from vpn_access_bot.handlers.payments import handle_pre_checkout_query


class _SlowReadiness:
    async def check(self, *, force: bool, timeout_seconds: float):
        _ = force, timeout_seconds
        await asyncio.sleep(1)
        return SimpleNamespace(can_sell=True)


class _Query:
    def __init__(self) -> None:
        self.invoice_payload = "order:test"
        self.total_amount = 60
        self.currency = "XTR"
        self.from_user = SimpleNamespace(id=100)
        self.answers: list[tuple[bool, str | None]] = []

    async def answer(self, *, ok: bool, error_message: str | None = None) -> None:
        self.answers.append((ok, error_message))


@pytest.mark.asyncio
async def test_precheckout_answers_before_slow_readiness_can_consume_telegram_deadline() -> None:
    query = _Query()
    settings = SimpleNamespace(
        pre_checkout_total_timeout_seconds=0.25,
        pre_checkout_answer_reserve_seconds=0.1,
        pre_checkout_readiness_timeout_seconds=0.05,
    )
    started = asyncio.get_running_loop().time()

    await handle_pre_checkout_query(
        query,  # type: ignore[arg-type]
        database=SimpleNamespace(),  # type: ignore[arg-type]
        settings=settings,  # type: ignore[arg-type]
        mediator_client=SimpleNamespace(),  # type: ignore[arg-type]
        readiness_service=_SlowReadiness(),  # type: ignore[arg-type]
        bot_key="primary",
    )

    elapsed = asyncio.get_running_loop().time() - started
    assert elapsed < 0.5
    assert len(query.answers) == 1
    assert query.answers[0][0] is False
