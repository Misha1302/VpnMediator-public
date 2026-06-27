# Reliability operations runbook

This runbook covers unresolved financial and entitlement states introduced by
the durable operation architecture. It intentionally avoids destructive repair
commands and raw secrets.

## Safety rules

- Run one VpnAccessBot writer process per SQLite database.
- Never disable Mediator admin authentication or entitlement version checks.
- Never change order/operation state directly in SQLite as the first response.
- Never retry an `external_unknown` operation with a new operation ID.
- Never log full provider charge IDs, Bot tokens, device tokens, subscription
  URLs, or VPN URIs.
- Capture read-only evidence before restart, rollback, or repair.

## Evidence to collect

Record UTC time, service status, process IDs, listening sockets, health/readiness,
current migration versions, and safe row identifiers for:

- payment inbox item;
- order public ID;
- entitlement/refund operation public ID;
- subscription public GUID;
- operation state, attempt count, timestamps, and safe error code;
- Mediator operation status and current entitlement version;
- outbox state;
- reconciliation quarantine state.

Do not paste provider references or credentials into tickets. Use the stored hash
or a short safe identifier.

## Unmatched payment

Symptoms: PaymentInbox state `manual_review`/`refund_required`, no matched order,
or an old unresolved inbox age.

Safe procedure:

1. Verify the provider charge is represented exactly once in PaymentInbox.
2. Compare payer, amount, currency, and invoice payload hash with the intended
   order without exposing the raw charge reference.
3. If the order can be proven, use the normal matching/application path.
4. Otherwise keep the payment durable and follow the controlled refund workflow.
5. Confirm the final inbox state and operator/user outbox result.

Never create a replacement payment record or mark the order paid without evidence.

## Stale entitlement operation

Symptoms: `pending`, expired `claimed`, `external_unknown`, `external_applied`, or
`local_commit_pending` older than the service-level objective.

Safe procedure:

1. Query the Mediator operation by the same stable operation ID.
2. If Mediator confirms the same normalized request was applied, run recovery to
   finish the local commit.
3. If Mediator proves no operation exists and local state is still compatible,
   retry with the same operation ID.
4. If payload identity conflicts or remote is newer for an unknown reason,
   quarantine and alert; do not generate a new ID.
5. Verify local/remote convergence and one terminal operation.

## Refund states

- `prepared`: provider call not yet known to have occurred.
- `provider_unknown`: do not call refund repeatedly without provider evidence;
  quarantine for manual verification.
- `provider_refunded`/`compensation_pending`: paid access must be revoked through
  the durable compensation operation.
- `completed`: provider refund and required compensation are both complete.
- `manual_review`: operator evidence is required.

Activation, renewal, referral, trial, and admin adjustment remain blocked while a
refund owns the subscription mutation lease.

## Reconciliation quarantine

Unknown remote-newer divergence blocks automatic mutation. First inspect the complete
local/remote snapshot and recent operations:

- `/reconcile_status <subscription-guid>`

Available audited repair modes are:

- `/reconcile_adopt_remote <subscription-guid> <reason>`
- `/reconcile_adopt_expired <subscription-guid> <remote-version> <reason>`
- `/reconcile_adopt_disabled <subscription-guid> <remote-version> <reason>`
- `/reconcile_restore_local <subscription-guid> <reason>`

`adopt_remote` accepts only unambiguous `active` or `expired` lifecycle states. It rejects an
unknown remote `disabled`, because that value may represent expiration, refund, abuse handling,
or an administrative revoke.

`adopt_expired` is the narrow compatibility repair for the historical state
`Subscription.expired + local entitlement active vN + remote disabled vN+1`. The local and
remote validity timestamp and device limit must match exactly, the validity must already have
ended, no entitlement mutation or unfinished order may exist, and the remote version must still
match the operator-confirmed snapshot. It mirrors the remote entitlement without issuing a new
Mediator operation while preserving the business lifecycle as `expired`. Repeating the command
for the same subscription, mode, and remote version reuses the completed repair operation.

`adopt_disabled` is an explicit operator decision for a confirmed refund, administrative revoke, abuse action, or another permanent denial. It requires the exact remote version, verifies that the current remote status is still `disabled`, refuses unfinished orders and active entitlement operations, and records the operator reason. It must not be used merely because the validity date has passed.

`/reconcile_status` also queries Mediator provenance by `(subscription, result_version)`. When available, the operation type and stable operation ID identify whether the remote version came from expiration, refund compensation, paid activation, or an administrative action. Missing provenance remains an ambiguity signal; it is not silently inferred from dates.

`restore_local` submits a normal version-checked operation and may conflict if state changes
again. It refuses to restore an `active` entitlement whose validity is already in the past. All
modes create audit and outbox evidence. Use no repair until the source of the remote mutation is
understood.

## Legacy `activating` order

Let automatic legacy recovery classify it. A retry is permitted only when the
captured base still matches local and remote state. Existing application evidence
is finalized without another remote apply. Ambiguous/new-subscription cases are
quarantined. Do not set `activation_failed` in bulk.

## Stale or unavailable catalog

Symptoms: readiness `degraded`/`not_ready`, catalog `stale`/`unavailable`, or zero
published servers.

1. Inspect enabled source count, last successful refresh and safe error codes.
2. Verify the upstream URL, DNS/TLS and response size without logging credentials or VPN URIs.
3. Confirm the latest valid source snapshot and published snapshot timestamps.
4. Do not manually mark a stale snapshot fresh.
5. Keep new purchases blocked until a fresh non-empty publication is persisted.
6. A recent stale snapshot may continue serving existing users only inside the configured age limit.

## Notification outbox

`provider_accepted_at_utc` means the Telegram API accepted the send call. It is
not proof of human delivery. Failed or stale `sending` rows are retried by the
outbox worker with the same idempotency key. Domain success must not be rolled
back merely because notification delivery failed.

## Production verification still required

This runbook does not prove real Telegram Stars payment/refund behavior, Happ
client behavior, VPN core routing, DNS/TLS/Nginx/systemd behavior, alert delivery,
or offsite restore. Those require controlled staging/production exercises.
