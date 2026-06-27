from __future__ import annotations

import pytest
from sqlalchemy import select

from vpn_access_bot import product_completion
from vpn_access_bot.db import Database
from vpn_access_bot.models import WorkerHealth


@pytest.mark.asyncio
async def test_critical_worker_stops_after_configured_failure_limit(monkeypatch) -> None:
    attempts = 0
    failures: list[str] = []

    async def failing_callback() -> None:
        nonlocal attempts
        attempts += 1
        raise RuntimeError("synthetic")

    async def record_failure(error_code: str) -> None:
        failures.append(error_code)

    async def no_sleep(_seconds: float) -> None:
        return None

    monkeypatch.setattr(product_completion.asyncio, "sleep", no_sleep)

    with pytest.raises(RuntimeError, match="exceeded its failure limit"):
        await product_completion._run_worker_loop(
            worker_name="critical-test",
            interval_seconds=1,
            callback=failing_callback,
            failure_callback=record_failure,
            critical=True,
            failure_limit=3,
        )

    assert attempts == 3
    assert failures == ["RuntimeError", "RuntimeError", "RuntimeError"]


@pytest.mark.asyncio
async def test_worker_loop_records_success_before_sleep(monkeypatch) -> None:
    events: list[str] = []

    async def callback() -> None:
        events.append("callback")

    async def record_success() -> None:
        events.append("success")

    async def stop_after_first_cycle(_seconds: float) -> None:
        raise product_completion.asyncio.CancelledError

    monkeypatch.setattr(product_completion.asyncio, "sleep", stop_after_first_cycle)

    with pytest.raises(product_completion.asyncio.CancelledError):
        await product_completion._run_worker_loop(
            worker_name="success-test",
            interval_seconds=1,
            callback=callback,
            success_callback=record_success,
            failure_callback=lambda _error: record_success(),
        )

    assert events == ["callback", "success"]


@pytest.mark.asyncio
async def test_worker_health_recording_failure_does_not_fail_business_cycle(monkeypatch) -> None:
    events: list[str] = []

    async def callback() -> None:
        events.append("callback")

    async def failing_success_record() -> None:
        events.append("health-failed")
        raise RuntimeError("health database unavailable")

    async def stop_after_first_cycle(_seconds: float) -> None:
        raise product_completion.asyncio.CancelledError

    monkeypatch.setattr(product_completion.asyncio, "sleep", stop_after_first_cycle)

    with pytest.raises(product_completion.asyncio.CancelledError):
        await product_completion._run_worker_loop(
            worker_name="health-failure-test",
            interval_seconds=1,
            callback=callback,
            success_callback=failing_success_record,
            failure_callback=lambda _error: failing_success_record(),
        )

    assert events == ["callback", "health-failed"]


@pytest.mark.asyncio
async def test_worker_recovery_persists_success_and_clears_old_error(tmp_path) -> None:
    database = Database(f"sqlite+aiosqlite:///{tmp_path / 'bot.db'}")
    await database.initialize()

    try:
        await product_completion._record_worker_health(
            database.session,
            "notification_outbox",
            success=False,
            error_code="OperationalError",
        )
        await product_completion._record_worker_health(
            database.session,
            "notification_outbox",
            success=True,
        )

        async with database.session() as session:
            health = (
                await session.execute(
                    select(WorkerHealth).where(WorkerHealth.worker_name == "notification_outbox")
                )
            ).scalar_one()

            assert health.last_success_at_utc is not None
            assert health.last_failure_at_utc is not None
            assert health.last_success_at_utc >= health.last_failure_at_utc
            assert health.last_error_code is None
    finally:
        await database.dispose()


def test_reconciliation_outbox_message_escapes_operator_snapshot() -> None:
    payload = (
        '{"username":"<admin>","telegram_id":"<1>",'
        '"public_guid":"<guid>","reason_code":"legacy_expiration_drift",'
        '"subscription_status":"expired","local_status":"active",'
        '"local_version":1,"local_valid_until_utc":"<local-date>",'
        '"local_max_device_tokens":3,"remote_status":"disabled","remote_version":2,'
        '"remote_valid_until_utc":"<remote-date>","remote_max_device_tokens":3,'
        '"suggested_action":"reconcile_adopt_expired <unsafe>"}'
    )

    message = product_completion._outbox_message("operator_reconciliation_blocked", payload)

    assert "<admin>" not in message
    assert "<guid>" not in message
    assert "<unsafe>" not in message
    assert "<local-date>" not in message
    assert "<remote-date>" not in message
    assert "&lt;admin&gt;" in message
    assert "&lt;guid&gt;" in message
    assert "&lt;local-date&gt;" in message
    assert "&lt;remote-date&gt;" in message
    assert "/reconcile_status &lt;guid&gt;" in message


def test_paid_order_reconciliation_alert_is_actionable_and_escaped() -> None:
    payload = '{"order_public_id":"<order>","public_guid":"<guid>","reason_code":"<reason>"}'

    message = product_completion._outbox_message(
        "operator_paid_order_reconciliation_blocked",
        payload,
    )

    assert "Платёж уже получен" in message
    assert "Не создавайте новый заказ" in message
    assert "&lt;order&gt;" in message
    assert "&lt;guid&gt;" in message
    assert "&lt;reason&gt;" in message
    assert "<order>" not in message
