# Current release traceability

| Requirement | Owner | Validation | Current local result |
|---|---|---|---|
| Unified feed is the only issuance path | Program/Bot client | architecture guard and .NET feed tests | PASS |
| Every valid upstream server is published | Mediator catalog | refresh regression test | PASS |
| Empty/stale catalog fails safely | Mediator readiness | .NET catalog/readiness tests | PASS |
| Device limit is atomic and idempotent | UnifiedSubscriptionFeed/repository | .NET concurrency tests | PASS |
| Payment/refund recovery remains unchanged | Bot services/repositories | full Python suite | PASS |
| Removed configuration cannot be silently ignored | updater/env examples | deployment guard | PASS |
| First-deploy rollback restores old units | updater | updater self-test + host canary | Static PASS; host not run |
| Bot artifact uses pinned build dependencies | updater/locks | wheel build/import | PASS |
| Two SQLite databases back up together | backup/restore scripts | restore self-test | PASS |
| No secrets or runtime data in source | release scripts | secret scan | PASS |
