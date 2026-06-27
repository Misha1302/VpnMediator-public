# Expiration policy

- Business timezone: `SUBSCRIPTION_TIME_ZONE` (default `Europe/Moscow`).
- Policy version: `EXPIRATION_POLICY_VERSION`.
- Effective date: `EXPIRATION_POLICY_EFFECTIVE_AT_UTC`.
- Existing active subscriptions are not changed automatically.

For a new duration order after the effective date:

```text
base = captured_now for first/resumed purchase
base = max(current_expiration, captured_now) for active renewal
nominal = base + purchased_duration
local_expiration = start of the calendar day after Date(nominal in business timezone)
target = local_expiration converted to UTC
```

The order stores `BaseExpiresAtUtc`, `PurchasedDuration`, policy version and immutable `TargetExpiresAtUtc`. Retry/reconciliation reuses the stored target. Device-only upgrade does not change expiration. The mediator applies the exact target with monotonic/version checks and never recalculates duration.

User UI displays the inclusive local date only. Happ expiration metadata remains configurable pending real-client evidence.

## Lifecycle state transition

Natural expiration is represented consistently as:

```text
Subscription.status      = expired
AccessEntitlement.status = expired
Mediator entitlement     = expired
```

`disabled` is reserved for an explicit revoke, refund compensation, abuse handling, or another
administrative access denial. Access is denied for both states, but only `expired` remains
eligible for the normal self-service resume flow.

Historical rows produced by the previous expiration implementation may contain
`Subscription.expired` with a remote `disabled` entitlement. They are not rewritten
heuristically. Operators must inspect the snapshot and use the guarded legacy reconciliation
repair described in `reliability-operations-runbook.md`.
