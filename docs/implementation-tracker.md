# Implementation tracker

> **Historical snapshot.** This tracker belongs to an earlier required-changes baseline. Its `BLOCKED` rows and test counts are not current release status. For the advertising-readiness artifact, use `docs/advertising-readiness.md`, `release/advertising-readiness-implementation-report.md` and the artifact-bound external release-evidence sidecar.

| Slice | Status | Automated evidence | Remaining evidence |
|---|---|---|---|
| P0-0 Baseline and release gate | `BLOCKED` | Python, shell, architecture, lock, secret and C# syntax checks | .NET 10 restore/build/test and online audits |
| P0-1 Private-chat boundary | `IMPLEMENTED_AND_VERIFIED` | boundary/support tests | Telegram group smoke |
| P0-2 Actor/payment ownership | `IMPLEMENTED_AND_VERIFIED` | authorization/payment/concurrency tests | Real Stars payment/refund |
| P0-3 Direct credential delivery | `BLOCKED` | Python credential tests; C# tests authored | .NET gate and real Happ clients |
| P0-4 Expiration | `IMPLEMENTED_AND_VERIFIED` | policy/migration/retry tests | Happ date presentation |
| P0-5 Commerce/idempotency | `IMPLEMENTED_AND_VERIFIED` | quote/payment/trial/race tests | Real payment outage scenario |
| P0-6 Network/backup | `IMPLEMENTED_AUTOMATED` | shell checks, local paired backup/restore, disk injection | external admin denial and production restore |
| P1-1 UX/presentation | `IMPLEMENTED_AUTOMATED` | menu/formatting/confirmation tests | five-person usability study |
| P1-2 Migrations/constraints | `IMPLEMENTED_AUTOMATED` | Python matrix/constraints | mediator .NET migration execution |
| P1-3 HTTP hardening | `BLOCKED` | C# source tests and syntax parser | .NET HTTP integration suite |
| P1-4 Storage/audit atomicity | `BLOCKED` | source review and syntax parser | .NET failure-injection tests |
| P1-5 Rate limiting/DoS | `BLOCKED` | authored C# tests | .NET/proxy load smoke |
| P1-6 Catalog model/sorting | `BLOCKED` | authored C# tests | .NET suite and Happ metadata smoke |
| P1-7 Catalog security | `BLOCKED` | source validation/anomaly/LKG implementation | .NET tests and upstream canary |
| P1-8 Cryptography lifecycle | `BLOCKED` | Python tests, secret scan, C# test source | .NET and key rotation drill |
| P1-9 Workers/health | `IMPLEMENTED_AND_VERIFIED` | supervisor/runtime/TCP health tests | staging outage/SIGTERM |
| P1-10 Observability | `IMPLEMENTED_AUTOMATED` | health, secret and shell checks | alert delivery |
| P1-11 Support/roles | `IMPLEMENTED_AND_VERIFIED` | support security tests | operator acceptance |
| P1-12 Deployment/capacity/DR | `EXTERNAL_EVIDENCE_REQUIRED` | hardened configs, tools, local restore/disk tests | real VPS/DNS/TLS/NTP/load/offsite/canary |
| P1-13 CI/supply chain | `BLOCKED` | SHA pins, 61-package hash lock, SBOM, secret scan | online audits and CI |
| P1-14 Retention/docs | `IMPLEMENTED_AUTOMATED` | cleanup tests and docs | real operator values |
| P1-15 Local E2E | `BLOCKED` | 105 Python tests and C# test source | real mediator test host |
| P2/P3 roadmap | `DEFERRED_BY_DESIGN` | scope review | post-beta only |
