from __future__ import annotations

import asyncio
import json

from sqlalchemy import text

from vpn_access_bot.readiness import CommerceReadinessService
from vpn_access_bot.telegram.bot_registry import BotRegistry


class BotHealthServer:
    def __init__(
        self,
        host: str,
        port: int,
        database,
        readiness: CommerceReadinessService,
        bot_registry: BotRegistry | None = None,
    ) -> None:
        self._host = host
        self._port = port
        self._database = database
        self._readiness = readiness
        self._bot_registry = bot_registry
        self._server: asyncio.Server | None = None

    async def start(self) -> None:
        self._server = await asyncio.start_server(self._handle, self._host, self._port)

    async def close(self) -> None:
        if self._server is None:
            return
        self._server.close()
        await self._server.wait_closed()
        self._server = None

    async def _handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            line = await asyncio.wait_for(reader.readline(), timeout=2)
            parts = line.decode("ascii", errors="ignore").split()
            path = parts[1] if len(parts) >= 2 else "/"
            while True:
                header = await asyncio.wait_for(reader.readline(), timeout=2)
                if header in {b"\r\n", b"\n", b""}:
                    break
            if path == "/health/live":
                await self._respond(writer, 200, {"status": "live"})
            elif path == "/health/ready":
                await self._ready(writer)
            elif path == "/health/commerce":
                await self._commerce(writer)
            elif path == "/metrics":
                await self._metrics(writer)
            else:
                await self._respond(writer, 404, {"status": "not_found"})
        except (TimeoutError, ConnectionError):
            writer.close()
        finally:
            if not writer.is_closing():
                writer.close()
            await writer.wait_closed()

    async def _database_ready(self) -> bool:
        async with self._database.session() as session:
            await session.execute(text("SELECT 1"))
        return True

    def _bots_ready(self) -> bool:
        return self._bot_registry is None or self._bot_registry.required_bots_ready

    async def _ready(self, writer: asyncio.StreamWriter) -> None:
        try:
            await self._database_ready()
        except Exception:
            await self._respond(
                writer,
                503,
                {"status": "not_ready", "reason": "database_unavailable"},
            )
            return
        bots_ready = self._bots_ready()
        body = {
            "status": "ready" if bots_ready else "not_ready",
            "processReady": bots_ready,
            "databaseReady": True,
            "bots": (
                self._bot_registry.health_snapshot() if self._bot_registry is not None else []
            ),
        }
        await self._respond(writer, 200 if bots_ready else 503, body)

    async def _commerce(self, writer: asyncio.StreamWriter) -> None:
        try:
            await self._database_ready()
            decisions = await self._readiness.all_decisions(force=True)
        except Exception:
            await self._respond(
                writer,
                503,
                {"status": "unavailable", "reason": "dependency_error"},
            )
            return
        payload = {
            "status": "available",
            "decisions": {
                decision.operation_kind.value: {
                    "allowed": decision.allowed,
                    "reasonCode": decision.reason_code,
                    "policyVersion": decision.policy_version,
                    "snapshotAtUtc": decision.snapshot_at_utc,
                    "facts": decision.facts or {},
                    "capacity": (
                        decision.capacity.to_public_dict()
                        if decision.capacity is not None
                        else None
                    ),
                }
                for decision in decisions
            },
        }
        await self._respond(writer, 200, payload)

    async def _metrics(self, writer: asyncio.StreamWriter) -> None:
        try:
            await self._database_ready()
            bots_ready = self._bots_ready()
            decisions = await self._readiness.all_decisions(force=True)
            lines = [
                "# TYPE vpn_access_bot_process_ready gauge",
                f"vpn_access_bot_process_ready {1 if bots_ready else 0}",
                "# TYPE vpn_access_bot_commerce_allowed gauge",
            ]
            for decision in decisions:
                operation = self._metric_label(decision.operation_kind.value)
                reason = self._metric_label(decision.reason_code)
                lines.append(
                    "vpn_access_bot_commerce_allowed"
                    f'{{operation_kind="{operation}",reason_code="{reason}"}} '
                    f"{1 if decision.allowed else 0}"
                )

            capacity = next(
                (decision.capacity for decision in decisions if decision.capacity is not None),
                None,
            )
            if capacity is not None:
                self._append_capacity_metrics(lines, capacity)

            mediator = next(
                (decision.mediator for decision in decisions if decision.mediator is not None),
                None,
            )
            if mediator is not None:
                published_servers = max(int(getattr(mediator, "server_count", 0)), 0)
                lines.extend(
                    [
                        "# TYPE vpn_access_bot_published_servers gauge",
                        f"vpn_access_bot_published_servers {published_servers}",
                    ]
                )

            if self._bot_registry is not None:
                lines.append("# TYPE vpn_access_bot_telegram_bot_ready gauge")
                for runtime in self._bot_registry.runtimes:
                    required = str(runtime.definition.required).lower()
                    bot_ready = 1 if runtime.status == "polling" else 0
                    lines.append(
                        "vpn_access_bot_telegram_bot_ready"
                        f'{{bot_key="{self._metric_label(runtime.key)}",required="{required}"}} '
                        f"{bot_ready}"
                    )
            async with self._database.session() as session:
                result = await session.execute(
                    text(
                        """
                        SELECT COALESCE(provider_payment_status, 'not_created'), COUNT(*)
                        FROM orders
                        WHERE provider = 'yookassa_sbp'
                        GROUP BY COALESCE(provider_payment_status, 'not_created')
                        """
                    )
                )
                lines.append("# TYPE vpn_access_bot_yookassa_orders gauge")
                for provider_status, count in result.all():
                    status_label = self._metric_label(str(provider_status))
                    lines.append(
                        "vpn_access_bot_yookassa_orders"
                        f'{{provider_status="{status_label}"}} {int(count)}'
                    )
            body_bytes = ("\n".join(lines) + "\n").encode()
            writer.write(
                b"HTTP/1.1 200 OK\r\nContent-Type: text/plain; version=0.0.4\r\n"
                + f"Content-Length: {len(body_bytes)}\r\nConnection: close\r\n\r\n".encode()
                + body_bytes
            )
            await writer.drain()
        except Exception:
            await self._respond(writer, 503, {"status": "not_ready"})

    @classmethod
    def _append_capacity_metrics(cls, lines: list[str], capacity: object) -> None:
        metrics = {
            "vpn_access_bot_capacity_utilization_percent": getattr(
                capacity, "utilization_percent", None
            ),
            "vpn_access_bot_active_subscriptions": getattr(capacity, "active_subscriptions", 0),
            "vpn_access_bot_active_devices": getattr(capacity, "active_devices", None),
            "vpn_access_bot_payment_inbox_pending": getattr(capacity, "payment_inbox_pending", 0),
            "vpn_access_bot_payment_inbox_oldest_age_seconds": getattr(
                capacity, "payment_inbox_oldest_age_seconds", None
            ),
            "vpn_access_bot_activation_pending": getattr(capacity, "activation_pending", 0),
            "vpn_access_bot_activation_oldest_age_seconds": getattr(
                capacity, "activation_oldest_age_seconds", None
            ),
            "vpn_access_bot_refund_pending": getattr(capacity, "refund_pending", 0),
            "vpn_access_bot_refund_manual_review": getattr(capacity, "refund_manual_review", 0),
            "vpn_access_bot_notification_backlog": getattr(capacity, "notification_backlog", 0),
            "vpn_access_bot_worker_stale_count": getattr(capacity, "worker_stale_count", 0),
        }
        for name, value in metrics.items():
            if value is None:
                continue
            lines.append(f"# TYPE {name} gauge")
            lines.append(f"{name} {cls._metric_number(value)}")
        state = cls._metric_label(str(getattr(capacity, "state", "unknown")))
        reason = cls._metric_label(str(getattr(capacity, "reason_code", "unknown")))
        lines.append("# TYPE vpn_access_bot_capacity_state gauge")
        lines.append(f'vpn_access_bot_capacity_state{{state="{state}",reason_code="{reason}"}} 1')

    @staticmethod
    def _metric_number(value: object) -> str:
        if value is None:
            return "0"
        if isinstance(value, float):
            return format(value, ".15g")
        return str(max(int(value), 0))

    @staticmethod
    def _metric_label(value: str) -> str:
        return value.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")

    @staticmethod
    async def _respond(
        writer: asyncio.StreamWriter, status: int, payload: dict[str, object]
    ) -> None:
        body = json.dumps(payload, default=str, separators=(",", ":")).encode()
        reason = {200: "OK", 404: "Not Found", 503: "Service Unavailable"}.get(status, "Error")
        headers = (
            f"HTTP/1.1 {status} {reason}\r\n"
            "Content-Type: application/json\r\n"
            "Cache-Control: no-store\r\n"
            f"Content-Length: {len(body)}\r\n"
            "Connection: close\r\n\r\n"
        ).encode()
        writer.write(headers + body)
        await writer.drain()
