# Retention policy

Default technical cleanup threshold is `CLEANUP_RETENTION_DAYS=90` and cannot be set below 30 by the cleanup command. The job supports `--dry-run` and is idempotent.

Eligible: expired unconsumed quotes, old product analytics events, completed/abandoned onboarding sessions, delivered/failed notification-delivery rows, completed/terminal notification-outbox rows, expired broadcast drafts and broadcast campaigns whose recipient/outbox records have already become removable. Broadcast drafts use `BROADCAST_DRAFT_RETENTION_HOURS` (24 hours by default); completed campaign bodies follow `CLEANUP_RETENTION_DAYS`.

Not deleted by this job: orders, payments/provider identifiers, order applications, entitlement segments/adjustments, audit events, support requests/messages, active/revoked device security records or subscription history. Those records require a separately approved legal/financial policy.

Run:

```bash
python -m vpn_access_bot.retention --dry-run
python -m vpn_access_bot.retention
```

Record counts, cutoff, operator, correlation/change ID and backup manifest before applying. Stop and investigate if counts differ materially from the dry run.
