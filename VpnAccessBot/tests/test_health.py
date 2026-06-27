from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from dataclasses import dataclass

import pytest

from vpn_access_bot.health import BotHealthServer


class FakeResult:
    def __init__(self, rows: list[tuple[str, int]] | None = None) -> None:
        self._rows = rows or []

    def all(self) -> list[tuple[str, int]]:
        return self._rows


class FakeSession:
    async def execute(self, statement: object) -> FakeResult:
        if "provider_payment_status" in str(statement):
            return FakeResult([("pending", 1), ("succeeded", 2)])
        return FakeResult()


class FakeDatabase:
    @asynccontextmanager
    async def session(self):
        yield FakeSession()


@dataclass(frozen=True)
class FakeReadinessResult:
    can_sell: bool
    mediator_ready: bool = True
    catalog_ready: bool = True
    checked_at_utc: str = "2026-06-07T00:00:00Z"
    reason: str | None = None


class FakeReadiness:
    async def check(self, *, force: bool = False) -> FakeReadinessResult:
        assert force is True
        return FakeReadinessResult(can_sell=True)


async def request(port: int, path: str) -> bytes:
    reader, writer = await asyncio.open_connection("127.0.0.1", port)
    writer.write(f"GET {path} HTTP/1.1\r\nHost: localhost\r\n\r\n".encode())
    await writer.drain()
    response = await reader.read()
    writer.close()
    await writer.wait_closed()
    return response


@pytest.mark.asyncio
async def test_health_server_returns_valid_no_store_json_response() -> None:
    server = BotHealthServer("127.0.0.1", 0, FakeDatabase(), FakeReadiness())
    await server.start()
    assert server._server is not None
    port = server._server.sockets[0].getsockname()[1]
    try:
        response = await request(port, "/health/live")
    finally:
        await server.close()

    head, body = response.split(b"\r\n\r\n", 1)
    assert head.startswith(b"HTTP/1.1 200 OK\r\n")
    assert b"Content-Type: application/json" in head
    assert b"Cache-Control: no-store" in head
    assert int(
        next(
            line.split(b":", 1)[1]
            for line in head.splitlines()
            if line.startswith(b"Content-Length:")
        )
    ) == len(body)
    assert body == b'{"status":"live"}'


class MetricsReadiness:
    async def all_decisions(self, *, force: bool = False):
        from types import SimpleNamespace

        assert force is True
        capacity = SimpleNamespace(
            utilization_percent=72.5,
            active_subscriptions=31,
            active_devices=44,
            payment_inbox_pending=2,
            payment_inbox_oldest_age_seconds=17,
            activation_pending=3,
            activation_oldest_age_seconds=29,
            refund_pending=1,
            refund_manual_review=1,
            notification_backlog=4,
            worker_stale_count=0,
            state="constrained",
            reason_code="capacity_constrained",
        )
        mediator = SimpleNamespace(
            server_count=9,
        )
        operation = SimpleNamespace(value="new_purchase")
        return [
            SimpleNamespace(
                operation_kind=operation,
                reason_code='policy_"disabled"',
                allowed=False,
                capacity=capacity,
                mediator=mediator,
            )
        ]


@pytest.mark.asyncio
async def test_metrics_expose_bounded_capacity_and_backlog_evidence() -> None:
    server = BotHealthServer("127.0.0.1", 0, FakeDatabase(), MetricsReadiness())
    await server.start()
    assert server._server is not None
    port = server._server.sockets[0].getsockname()[1]
    try:
        response = await request(port, "/metrics")
    finally:
        await server.close()

    head, body = response.split(b"\r\n\r\n", 1)
    assert head.startswith(b"HTTP/1.1 200 OK\r\n")
    metrics = body.decode()
    assert (
        'vpn_access_bot_commerce_allowed{operation_kind="new_purchase",'
        'reason_code="policy_\\"disabled\\""} 0' in metrics
    )
    assert "vpn_access_bot_capacity_utilization_percent 72.5" in metrics
    assert "vpn_access_bot_active_subscriptions 31" in metrics
    assert "vpn_access_bot_active_devices 44" in metrics
    assert "vpn_access_bot_payment_inbox_pending 2" in metrics
    assert "vpn_access_bot_payment_inbox_oldest_age_seconds 17" in metrics
    assert "vpn_access_bot_activation_pending 3" in metrics
    assert "vpn_access_bot_refund_pending 1" in metrics
    assert "vpn_access_bot_refund_manual_review 1" in metrics
    assert "vpn_access_bot_notification_backlog 4" in metrics
    assert "vpn_access_bot_worker_stale_count 0" in metrics
    assert (
        'vpn_access_bot_capacity_state{state="constrained",'
        'reason_code="capacity_constrained"} 1' in metrics
    )
    assert "vpn_access_bot_published_servers 9" in metrics
    assert "# TYPE vpn_access_bot_yookassa_orders gauge" in metrics
    assert 'vpn_access_bot_yookassa_orders{provider_status="pending"} 1' in metrics
    assert 'vpn_access_bot_yookassa_orders{provider_status="succeeded"} 2' in metrics
    assert "user_id" not in metrics
    assert "order_id" not in metrics
