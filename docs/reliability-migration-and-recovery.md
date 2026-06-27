# Reliability migrations and recovery

This document describes the additive reliability migrations introduced after the
baseline archive SHA-256
`31a7275faafdd96b8f2991f0a81c1460319e9705de9ce4ec3c7d2ae2127f49c3`.
It is an operator/developer runbook, not proof that production data has already
been migrated.

## Schema versions

### VpnAccessBot SQLite

The Bot schema advances from version 13 to version 19.

| Version | Name | Purpose |
|---:|---|---|
| 14 | `durable_payment_inbox` | Append-first payment evidence with provider-charge uniqueness and reconciliation state. |
| 15 | `durable_entitlement_and_refund_operations` | Durable entitlement/refund state machines, one active mutation per subscription, and reconciliation quarantine metadata. |
| 16 | `notification_timestamps_and_transactional_outbox` | Separates claim/send/provider acceptance from delivery and adds the notification outbox. |
| 17 | `trial_reservation_and_refunding_order_state` | Separates trial reservation from usable period and treats `refunding` as an open order. |
| 18 | `recoverable_subscription_creation_result` | Persists the externally created subscription GUID on the operation for restart recovery. |
| 19 | `refunding_order_domain_state` | Rebuilds domain-state triggers so `refunding` is legal and constrained. |

### VpnMediator SQLite

The Mediator schema advances from version 14 to version 16.

| Version | Purpose |
|---:|---|
| 15 | Durable entitlement-operation request/result journal committed atomically with entitlement mutation. |
| 16 | Persisted source/health/publication counts, source snapshot identity, fallback flag/reason, and health evaluation timestamp. |

All migrations are additive. Existing tables are not blindly rewritten except
for the notification-delivery semantic migration, which preserves historical
provider acceptance while clearing the unsupported claim that Telegram proved
actual user delivery.

## Required pre-migration procedure

1. Stop the single Bot writer before copying its SQLite database. Do not start a
   second Bot process against the same database.
2. Back up both Bot and Mediator databases independently, including WAL/SHM when
   using a live SQLite backup mechanism.
3. Record file hashes, sizes, UTC time, service versions, and current migration
   rows.
4. Run the migration on a copied database first.
5. Start Mediator before Bot so operation-query and idempotent apply endpoints are
   available to recovery.
6. Do not restore a database automatically during a code rollback. A database
   restore can erase payments and operations created after the backup.

## Existing `activating` orders

Startup recovery does not mass-convert `activating` orders to
`activation_failed` and does not blindly resend them.

The classifier uses these rules:

- If an `OrderApplication` already exists, local commerce finalization can be
  completed without replaying the remote entitlement mutation.
- If a previous-subscription operation has a captured base version and both the
  local mirror and current remote entitlement still match that base exactly, a
  synthetic durable operation may continue the original purchased intent.
- If remote state is newer, the payload is ambiguous, or the operation is a new
  subscription with no persisted remote identity, the order is placed in manual
  review/quarantine and an operator outbox alert is created.
- The classifier never disables entitlement-version validation and never invents
  a version.

## Frozen paid orders

Unpaid stale quotes/orders should be replaced with a new quote. For a paid order,
recovery uses immutable purchased terms (`purchased_duration_days`, operation
kind, and requested device-limit semantics) and applies them over the latest
authoritative entitlement. An absolute target timestamp is audit evidence, not
the source of the result.

If the purchased delta cannot be reconstructed unambiguously, the order is not
applied automatically. It is quarantined for operator review.

## Entitlement divergence

Reconciliation classifies state before acting:

- Known unfinished operation already applied remotely: finish the local commit.
- Known unapplied local operation: retry idempotently using the same operation ID.
- Same version and payload: healthy.
- Remote newer with unknown origin: quarantine, block new automatic mutation,
  and create a deduplicated operator alert.
- Explicit operator repair is limited to:
  - `adopt_remote`: copy the fetched authoritative remote result locally;
  - `restore_local`: submit an audited durable operation with normal version
    validation.

Both repair modes require an administrator actor and reason. There is no automatic
unknown-remote overwrite.

## Trial migration

Historical trial rows backfill `reserved_at_utc` from creation and
`usable_started_at_utc` from the old `started_at_utc`. New trials reserve the
anti-abuse claim first but begin the promised duration only after confirmed
entitlement application.

## Notification migration

Historical rows previously labelled `delivered` are migrated to
`provider_accepted`. Their old timestamp is retained as
`provider_accepted_at_utc`; `delivered_at_utc` is cleared because the Telegram Bot
API does not prove that the user read or received the message. New domain commits
write a transactional outbox row, and dispatch records provider acceptance only
after a successful API call.

## Health snapshot migration

Old published snapshots remain readable. Their new health/fallback metadata may
be unknown until the next source refresh or health publication. Readiness must not
interpret unknown metadata as fully healthy. Every new health-only publication is
reconstructed from the exact full source snapshots referenced by the latest
source-derived catalog, never from the previous filtered publication.

## Rollback constraints

- Application rollback is permitted only within the tested additive-schema
  compatibility window.
- Do not delete operation, payment inbox, refund, quarantine, or outbox rows to
  make an older binary start.
- Do not downgrade migration rows.
- If rollback cannot read the additive schema, keep the new database intact and
  restore service from a tested compatible binary; database restore is an
  incident decision requiring payment/operation reconciliation.
