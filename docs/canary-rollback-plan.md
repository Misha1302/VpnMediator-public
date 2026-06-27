# Canary and rollback plan

Stages: production-like database copy → old bot with new mediator → internal accounts → small beta cohort → gradual expansion. Take a consistent two-database pre-change backup and retain previous binaries/config before each stage.

Required Device Issuance v2 canary scenarios:

1. An existing legacy URL still refreshes the catalog.
2. One onboarding retry returns the same public device ID and occupies one slot.
3. Two Android onboarding sessions return different device IDs and occupy two slots.
4. A revoked issuance request does not create a hidden replacement.
5. Explicit regeneration invalidates only the selected device and preserves the replacement identity.
6. `/health/ready` advertises `deviceIssuanceVersion: 2` before the bot starts.

Rollback triggers include unauthorized ownership, duplicate entitlement, duplicate issuance for one idempotency key, unresolved payment application, catalog zero/unexpected replacement, token failures, 5xx/429 spikes, SQLite lock errors, worker death or abnormal support volume.

Ordinary rollback switches application code/venv back and leaves additive schema changes in place. Do not restore the two-database backup automatically: doing so may delete orders, payments, entitlements or tokens created after deployment. Database restore requires an explicit incident decision and reconciliation plan.

After rollback verify readiness, bot stability, one existing feed refresh, one revoked-token denial, catalog freshness, and the actual deployed release marker. Reconcile every payment received during the incident.
