# Current capability matrix

| Capability | State |
|---|---|
| Telegram Stars purchase/payment/refund state machines | Implemented; full regression suite required |
| YooKassa SBP checkout | Implemented behind disabled feature flags; sandbox and receipt canary required |
| One stable Happ feed URL per subscription | Implemented |
| HMAC-hashed Happ HWID and atomic device limit | Implemented |
| Existing per-device links during compatibility window | Read-only consumption retained |
| New personal link issuance/regeneration/transfer | Removed |
| Shared upstream catalog refresh and stale fallback | Implemented |
| Server reachability filtering and latency ranking | Removed by product decision |
| Managed provider provisioning | Removed |
| Bot and Mediator SQLite migrations | Bot schema 34; Mediator schema 24 |
| Two-database coordinated backup/restore | Implemented; target-host drill required |
| Atomic two-service release symlinks and rollback | Implemented; target-host canary required |
| Production approval | Not established locally |
