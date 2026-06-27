# VpnMediator Closed Beta Implementation Plan

Historical note: this file records an earlier implementation plan and contains stale baseline
observations. Current runtime behavior is documented in `README.md`,
`VpnAccessBot/README.md`, and `docs/deployment-guide.md`.

## Verified Starting Point

- Branch: `main`
- Initial HEAD: `d5a4b34a57e763d5469e2d428354ae3fb13af3fb`
- Local branch state: behind `origin/main` by 3 commits
- Working tree: tracked files clean; untracked `combine_vpnmediator_code.sh` and `combined_code.txt` preserved
- Reference baseline diff: no local tracked difference from the requested baseline commit

## Baseline Architecture

- `VpnMediator` is a .NET 10 minimal API with all domain, repository and helper code in `Program.cs`.
- Mediator persistence is a JSON file configured by `VpnMediator:DatabasePath`.
- Public subscription access uses signed master links and fallback device fingerprinting.
- Public subscription access currently fetches the upstream subscription URL directly.
- `VpnAccessBot` is an aiogram 3 / SQLAlchemy asyncio bot using SQLite and fixed seeded tariffs.
- The bot currently knows `UPSTREAM_SUBSCRIPTION_URL` and applies commercial state through rent, limit, enable and disable calls.

## Execution Slices

1. Storage and migration
   - Add versioned bot migrations and remove `create_all()` as the production initialization strategy.
   - Add mediator SQLite storage with deterministic schema migrations.
   - Add JSON to SQLite import with timestamped backup and legacy link preservation.

2. Payment safety
   - Add stable public order IDs, quote snapshots and order application records.
   - Add safe state transitions and idempotent activation boundaries.
   - Add technical refund eligibility that refuses automatic refunds after successful application.

3. Entitlement contract
   - Add bot `AccessEntitlement`.
   - Add mediator `EntitlementMirror`.
   - Implement `PUT /admin/subscriptions/{publicGuid}/entitlement`.
   - Add idempotent `POST /admin/subscriptions`.

4. Flexible quote-driven purchase
   - Add pricing configuration and deterministic pricing.
   - Add quote creation, expiration and order snapshot logic.
   - Preserve old tariff compatibility while moving new flow to quotes.

5. Russian state-driven UX
   - Centralize user/admin/error text.
   - Add `CabinetState` read model.
   - Replace generic buy/renew menu labels with state-specific actions.

6. Device tokens
   - Add mediator device token table and public token URL.
   - Store only token hashes, return raw token only on creation.
   - Add list/revoke/revoke-all APIs and bot client methods.
   - Keep legacy signed links working during the migration period.

7. Happ setup
   - Add platform setup texts and plain URL fallback.
   - Add capability research document.

8. Common server catalog
   - Add upstream source model, encrypted endpoint storage and staged lifecycle.
   - Add SSRF-safe fetcher and generic `subscription_url` reader.
   - Keep future source kinds as registry boundaries only.

9. Snapshots and resilience
   - Add source and published snapshots.
   - Make public subscription reads snapshot-only.
   - Add refresh, last-known-good and rollback operations.

10. Support, notifications and admin
   - Add support diagnostics without secrets.
   - Add notification deduplication records and services.
   - Add Russian admin responses and audit diagnostics.

11. Operations, monitoring and CI
   - Add health/readiness endpoints.
   - Add audit events.
   - Add systemd/env/backup assets and GitHub Actions.

12. Future boundaries
   - Keep future provider/source work generic and documented.
   - Do not add brand-specific fake provider implementations.

## Compatibility Decisions

- Existing signed legacy links remain valid but are governed by local entitlement state.
- New subscriptions receive explicit device-token URLs.
- Existing per-subscription upstream URLs are imported only as draft upstream sources and are not published automatically.
- Bot no longer needs to know upstream source URLs for new activation.
- Deprecated rent/limit/enable/disable endpoints remain as wrappers for old clients.

## Verification Strategy

- Run .NET restore/build/test after mediator changes.
- Run bot compile, Ruff and pytest after bot changes.
- Add focused tests for pricing, text formatting, entitlement idempotency, token security helpers, SSRF classification and migrations.
- Run `git diff --check` before final report.
