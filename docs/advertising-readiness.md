# Advertising readiness implementation

**Baseline:** `VpnMediator-ea428d9.tar.gz` supplied on 2026-06-17.
**Verified SHA-256:** `48fd3c997e6f3a62664a54233a1ecd9aef2aca30098a39e0305cc0f1bf278f6a`.
**Provenance note:** this uploaded archive hash differs from the hash written in the planning document, so the uploaded archive is the authoritative implementation baseline.  
**Scope:** locally implementable advertising-readiness requirements.  
**Production status:** source implementation and local validation do not prove a production GO.

## Implemented guarantees

### Refund correctness

- Every admin refund starts with an immutable `RefundPlan` preview.
- The preview stores the exact target status, expiry, device limit, entitlement version and an evidence hash.
- `/confirm_refund TOKEN` is one-time, actor-bound and expires after the configured TTL.
- `purchase`, `extend`, `upgrade_devices` and `extend_and_upgrade` compensate only the contribution of the refunded order.
- A newer entitlement mutation between preview and confirmation blocks the refund before the Telegram provider call.
- Provider success is persisted before local compensation and recovered without a second provider refund.
- Legacy orders without enough evidence are not guessed; they are rejected or sent to manual review before a provider effect.

### Referral lifecycle

- Referral rewards are linked to their source order.
- Maturity and application revalidate that the source order is still paid and applied.
- Pending or available rewards are cancelled when the source order enters refunding.
- Applied rewards enter durable reversal and remove only their own entitlement segment.
- New referral creation is disabled by default in deployment examples until the real refund/reversal canary passes.

### Commerce admission and operator control

Admission is evaluated per operation:

- `new_purchase`
- `trial`
- `renewal`
- `resume`
- `upgrade_devices`
- `extend_and_upgrade`
- `complete_paid_order`
- `retry_activation`
- `refund_prepare`
- `refund_compensation`
- `issue_existing_feed`

New obligations require a fresh safe catalog, the configured healthy-server floor and, when enabled, available capacity. Already accepted payments and refund compensation bypass the general new-sales admission gates.

Dynamic switches are stored in `commerce_policy` and changed without restart. `upgrade_devices` and `extend_and_upgrade` are independent fail-closed switches:

```text
/commerce_status
/commerce_stop new_purchases REASON
/commerce_start new_purchases REASON
/commerce_stop trials REASON
/commerce_stop referrals REASON
/commerce_start capacity_enforcement REASON
/confirm_commerce TOKEN
/capacity_status
```

`/commerce_start` and `/commerce_stop` only prepare a five-minute, actor-bound, one-time change request. The policy is mutated only by `/confirm_commerce TOKEN`. Confirmation uses an expected policy version, so a stale request cannot overwrite a newer operator decision. Prepared and confirmed operations are audited separately and survive process restart.

### Health contracts

- `/health/live` reports process liveness.
- `/health/ready` reports process/database/required-bot readiness and is not made unhealthy merely because sales are stopped.
- `/health/commerce` reports operation-specific decisions, reason codes, policy version and bounded capacity evidence.
- `/metrics` exposes bounded operation-kind and reason-code labels plus capacity, payment/activation backlog ages, refund/manual-review, notification backlog, stale workers and healthy published-server gauges; user/order IDs are not metric labels.

### Capacity admission

`CapacityService` combines Bot DB backlog/state with bounded Mediator capacity fields. It reports:

- active subscriptions and active devices;
- configured subscription/device capacity;
- pending and oldest payment/activation work;
- refund/manual-review and notification backlog;
- stale workers;
- `healthy`, `constrained`, `saturated` or `unknown`.

Transitions are persisted. High and low watermarks plus minimum dwell time prevent rapid open/close oscillation. Unknown capacity denies new capacity-sensitive operations once `capacity_enforcement` is enabled. Deployment examples intentionally leave numeric capacity at zero so an operator must enter measured values before enabling enforcement.

### Campaign attribution and cohort funnel

- Campaigns use opaque public tokens: `https://t.me/RazaltushVpnBot?start=c_<token>`.
- First touch is immutable; last touch follows the explicit latest-valid-campaign policy.
- Campaign attribution and referral binding remain separate concepts.
- Repeated `/start` is idempotent, including repeated calls in one transaction.
- `/product_funnel [DAYS]` follows the same anchored user cohort through a seven-day conversion horizon.
- The event registry uses actual producer names such as `subscription_feed_issued` and `device_limit_denied`.

Admin commands:

```text
/campaign_create CHANNEL [PLACEMENT] [CREATIVE]
/campaign_status TOKEN
/product_funnel [DAYS]
```

### Public branding and server presentation

- `@RazaltushVpnBot` is the canonical generated public support identity.
- The default deployment templates configure only the canonical bot. A historical bot key may remain in trusted compatibility routing, but it is not emitted as the public contact.
- Startup bot identity is checked against configured Telegram `getMe` identity.
- Catalog presentation version 5 normalizes only allowlisted semantic connection labels:

```text
1 | Германия мобильный интернет
2 | Польша
3 | Нидерланды вайфай
```

- Final numbering is assigned after filtering/ranking.
- Within the same health class, mobile-internet servers are listed first, neutral servers next,
  and Wi-Fi servers last. Health eligibility and health state still take precedence.
- Arbitrary upstream display text and old ordinals do not leak into the public name.
- Technical URI identity/content fingerprint remains unchanged by presentation-only changes.
- `scripts/check-public-branding.py` blocks stale public branding in generated/user-facing source and deployment templates.

### Device wording

User-facing actions describe the actual guarantee: the service can deny future subscription refresh for a device and release a slot, but cannot promise immediate revocation of already imported raw upstream credentials.

### User-operation serialization

State-changing Telegram presentation actions are serialized by `(bot_key, user_id)`. Different users remain concurrent. Deadline-sensitive pre-checkout and durable successful-payment ingestion are intentionally outside this presentation lock and remain protected by their existing database/idempotency boundaries.

## Schema changes

Bot migrations are additive:

- **27** — operation-specific `commerce_policy`;
- **28** — immutable `refund_plans` and order before-snapshots;
- **29** — referral cancellation/reversal lifecycle fields;
- **30** — campaigns, first/last-touch attribution and touch evidence;
- **31** — capacity state transitions;
- **32** — durable, actor-bound commerce-policy change confirmations with optimistic policy-version checks and conservative pre-advertising defaults for untouched compatibility rows.
- **33** — independent fail-closed `extend_and_upgrade_enabled` policy; a legacy enabled device-upgrade switch does not silently expose the combined operation.
- **34** — YooKassa/SBP checkout state, exact alternate-currency quote inputs, and one-order-per-quote enforcement.

Mediator schema remains version 24. Catalog presentation version is 4.

Do not downgrade to a bot binary that cannot understand provider-refunded operations or schema 27-34. Database rollback means restoring the paired pre-deploy backup, not dropping the additive tables in place.

### Stateful lifecycle validation

`scripts/run-stateful-lifecycle-harness.sh` exercises the commercial lifecycle against real Bot persistence and domain services with controlled fake external adapters. It runs concurrent refunds, injects a lost response after the remote entitlement commit, disposes and reopens the database to model restart, then retries recovery. The invariants assert one provider refund per charge, one remote application per idempotency key, no pending local operation and exact order-scoped entitlement rollback.

### Artifact-bound release evidence

The package builder produces four mutually checked files: the archive, `.sha256`, `.manifest.json` and `.release-evidence.md`. The external evidence sidecar contains the final artifact SHA-256; the manifest contains both the artifact and evidence hashes. `scripts/verify-release-bundle.py` rejects a changed archive, checksum, manifest or evidence file, and `scripts/release-evidence-self-test.sh` verifies that tampering is detected.

## Deployment sequence

1. Verify the archive, `.sha256`, `.manifest.json` and external `.release-evidence.md` together with `scripts/verify-release-bundle.py`; the in-archive evidence records source gates, while the external sidecar binds those gates to the final archive SHA.
2. Quiesce the Bot and Mediator writers, create the coordinated pair of SQLite snapshots with exact migration-set fingerprints, run the strict restore drill and copy the backup off-host.
3. Capture effective systemd units, drop-ins and environment-file provenance without printing secrets.
4. Configure `@RazaltushVpnBot` as the canonical public username and remove stale effective overrides.
6. Set measured `CONFIGURED_SUBSCRIPTION_CAPACITY` and/or `CONFIGURED_DEVICE_CAPACITY`.
7. Keep `TRIAL_ENABLED=false` and `REFERRAL_ENABLED=false` for the first canary.
8. Deploy and apply additive migrations.
9. Verify `/health/live`, `/health/ready` and `/health/commerce`.
10. Verify `/commerce_status`; keep new purchases stopped until the real smoke tests pass.
11. Complete one real Stars purchase, duplicate update, activation, Happ fetch and real refund.
12. Verify the existing and new Happ subscriptions show `@RazaltushVpnBot`, final-order numbering and the required postfixes.
13. Enable `capacity_enforcement` only after measured capacity is entered and shadow decisions are understood.
14. Start a budget- and rate-limited advertising canary with explicit stop conditions.

## Rollback

Before mutation, retain:

- bot and Mediator database backups from the same timestamp;
- previous binaries/configuration;
- current effective environment provenance;
- artifact and source hashes.

If no provider-refunded or other new financial operation has been created, roll back binary/configuration and restore the paired databases if required. Once a provider refund has succeeded under the new version, rolling back to code that does not understand its compensation state can be more dangerous than a forward fix. Stop new commerce, keep recovery workers on the compatible version and resolve the operation first.

## Conditions still requiring real evidence

Local tests cannot prove:

- Telegram Stars refund outcome and timeout semantics;
- real Happ first fetch, refresh and imported-credential behavior;
- strict credential revoke in `shared_catalog`;
- actual upstream VPN capacity and client-network reachability;
- production DNS/TLS/Nginx/systemd behavior;
- offsite restore, OOM/SIGKILL and alert delivery;
- real advertising conversion, refund and support rates.

Wide paid advertising remains **NO-GO** until these conditions pass a monitored canary. The locally validated artifact is a candidate for controlled deployment, not proof of production readiness.
