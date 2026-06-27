# Load-test report

## Initial envelope

- 1,000 active subscriptions.
- 100 subscription refresh requests/minute sustained.
- Burst of 20 requests/second.
- 20 simultaneous commerce operations.
- Catalog up to `ServerCatalogMaxServers`.

This is a test envelope, not promised capacity.

## Tools

Use `scripts/load-test.py` only with staging/test credentials and never publish URLs containing bearer tokens. Run public subscription fetch, loopback credential retrieval, catalog refresh and backup timing separately. Threshold defaults: zero 5xx and p95 below 500 ms for the HTTP smoke.

The commercial lifecycle is covered locally by the full Python suite: durable update/payment inbox, duplicate callbacks, concurrent state mutations, refund recovery, referral reversal, attribution idempotency and capacity hysteresis. This is state-machine evidence, not a production throughput measurement. A real staging run must still combine the HTTP smoke with purchase/activation/refund workers and capture backlog age plus SQLite busy errors.

## Local evidence

The sandbox executed the complete Bot lifecycle suite, SQLite concurrency tests, `scripts/disk-full-test.py`, a warning-as-error .NET build and the full Mediator test suite. Production-like HTTP/load results remain `EXTERNAL_EVIDENCE_REQUIRED` because no real deployment host, Telegram provider or Happ client was used.

Record CPU/RAM, DB lock errors, p50/p95/p99, throughput, catalog size, backup duration and rollback trigger decisions in the release evidence matrix.
