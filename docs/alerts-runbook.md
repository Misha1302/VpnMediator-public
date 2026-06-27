# Alerts runbook

Critical alerts: mediator not ready, zero/stale catalog, paid-but-not-applied age, dead critical worker, stale backup, repeated 5xx/admin 401, low disk and rejected catalog anomaly.

Use `scripts/alert-fire-drill.sh` with explicit confirmation to emit a synthetic journald critical event. Verify receipt in the real alert channel, acknowledge it, attach timestamps/screenshots and close the incident. Merely writing to journald is not delivery evidence.

Recommended initial thresholds: readiness failure over 2 minutes; catalog stale over configured maximum; paid-but-not-applied over 5 minutes; critical heartbeat over two intervals; backup over 30 hours; disk free below 15%; sustained 5xx over 1% for 5 minutes. Tune from staging/load evidence.
