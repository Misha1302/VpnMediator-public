# Required Changes Implementation Progress

> **Historical snapshot.** The verification counts and environment limitations below describe an earlier implementation pass. They must not be used as current release evidence. Current status is recorded in `docs/advertising-readiness.md` and the artifact-bound release-evidence sidecar.

This file records the implementation of `vpnmediator_required_changes_spec(1).md` against the
provided source archive. The archive did not contain `.git`; the temporary local baseline commit is
not a claim about the upstream branch or commit.

## Completed slices

### Slice 0 — baseline and guardrails

- [x] Archive paths and links checked before extraction.
- [x] Python baseline reproduced.
- [x] Architecture guard and full release-validation script added.
- [x] Regression tests added for confirmed defects.

### Slice 1 — state model and home renderer

- [x] Subscription lifecycle and unfinished-order notice are independent.
- [x] Active access remains visible with pending or failed renewal.
- [x] Main menu is rendered from one computed home state.
- [x] User handlers no longer construct an unqualified default menu.

### Slice 2 — concurrency and activation safety

- [x] Telegram user creation uses SQLite upsert.
- [x] Trial eligibility is centralized.
- [x] Trial acquisition uses conditional SQL and a stable idempotency key.
- [x] User entitlement mutations use a durable database lease.
- [x] Mediator calls run outside long SQLite write transactions.
- [x] Trial and paid activation finalization roll back before recording retryable failure.
- [x] Onboarding completion is conditional and idempotent.

### Slice 3 — catalog, pricing and readiness

- [x] Product period/device options have one validated source of truth.
- [x] Pricing fingerprint is persisted in quotes/orders.
- [x] Purchase, renew, resume and device upgrade have explicit validation rules.
- [x] Crafted unsupported values are rejected server-side.
- [x] Readiness guards trial, invoice, pre-checkout, manual approval and activation retry.

### Slice 4 — commerce UX

- [x] Purchase, renewal, resume and device-limit upgrade are separate flows.
- [x] Renewal preserves the current device limit.
- [x] Upgrade shows only larger supported limits.
- [x] `Дальше: X` controls were removed.
- [x] Exact public order IDs are used for cancel/continue/retry actions.

### Slice 5 — onboarding

- [x] Trial/payment success leads directly to device onboarding.
- [x] QR and duplicate handoff actions were removed.
- [x] The first-fetch check is shown only after direct credential issuance.
- [x] Dynamic browser handoff is removed; old `GET /connect/*` links return a no-store `410 Gone` tombstone without changing state.
- [x] Browser polling uses a secret-authorized coarse public state.
- [x] Completed onboarding sessions stop reminders.

### Slice 6 — mediator subscription output

- [x] Healthy subscriptions contain only real published server links.
- [x] Blocking state produces at most one compatibility entry.
- [x] Old tests that required fake status servers were replaced.

### Slice 7 — localization and product analytics

- [x] User-visible order/payment statuses are localized.
- [x] Raw exception messages are excluded from Telegram responses.
- [x] Moscow-time formatting is centralized.
- [x] Append-only product events cover the main funnel without storing secrets.
- [x] `/product_funnel [DAYS]` exposes a minimal admin funnel report.
- [x] Onboarding and trial reminders are idempotent.

### Slice 8 — production hardening

- [x] Application and Nginx rate limits added.
- [x] Historical handoff tables remain migration-safe but are no longer reachable from runtime endpoints.
- [x] Architecture guards reject reintroduction of dynamic handoff creation, redeem or status APIs.
- [x] Correlation IDs propagate between bot, proxy and mediator.
- [x] Admin-token rotation window documented and implemented.
- [x] SQLite WAL, synchronous mode and busy timeout configured.
- [x] Paired backup restore drill added.
- [x] Telegram admin alerts cover readiness, paid activation failures and stale workers.
- [x] Release and support runbooks updated.

## Verification performed in the packaging environment

- Python compile: passed.
- Ruff lint: passed.
- Python tests: 53 passed.
- Shell syntax: passed.
- Architecture guard: passed.

## Verification requiring the target/CI environment

- .NET 10 restore, build and tests: not executable in the packaging environment because the SDK is
  unavailable; GitHub Actions and `scripts/validate-release.sh` enforce them.
- ShellCheck: enforced in CI; not installed in the packaging environment.
- Real Happ smoke matrix: requires Android, iOS, desktop and advertised TV devices.
- External alert delivery requires deployment-specific monitoring data.
