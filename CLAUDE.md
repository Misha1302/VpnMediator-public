# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

**Язык общения:** отвечай пользователю на русском языке.

## Services

This monorepo contains three production services:

- **VpnMediator** — ASP.NET Core 10 (`Program.cs` is a single large file, no controllers). Manages subscriptions, HWID device registry, upstream catalog, and server health. SQLite via `Microsoft.Data.Sqlite`.
- **VpnAccessBot** — Python/aiogram 3 Telegram bot. Commerce, payments, user onboarding. SQLite via SQLAlchemy async + aiosqlite.
- **VpnProbeAgent** — Minimal Python process. Exposes a Unix socket for VPN server connectivity probing via Xray. Least-privilege design.

`VpnProvisioning.Contracts` is a shared C# contracts project referenced by both the mediator and tests.

## Local validation commands

### Python (VpnAccessBot)
```bash
cd VpnAccessBot
python3 -m venv .venv && source .venv/bin/activate
pip install --require-hashes -r requirements.lock
pip install --no-deps -e .

python -m compileall vpn_access_bot tests
ruff check .
ruff format --check .
python -m pytest
```

Run a single test file:
```bash
python -m pytest tests/test_buy.py -v
```

### Python (VpnProbeAgent)
```bash
cd VpnProbeAgent
# Uses VpnAccessBot's venv/lock
pip install --require-hashes -r ../VpnAccessBot/requirements.lock
pip install --no-deps -e .
python -m pytest
```

### .NET (VpnMediator)
```bash
dotnet restore VpnMediator.Tests/VpnMediator.Tests.csproj
dotnet build VpnMediator.Tests/VpnMediator.Tests.csproj --configuration Release --no-restore -warnaserror
dotnet test VpnMediator.Tests/VpnMediator.Tests.csproj --configuration Release --no-build --no-restore
```

Offline/no-network restore: add `-p:NuGetAudit=false`.

Validate options only (no HTTP server start):
```bash
dotnet run -- --validate-options-only
```

## Architecture

### Responsibility split
The bot is the source of **commercial truth**: users, tariffs, quotes, orders, subscription lifetime. The mediator is the source of **technical delivery truth**: subscription feed validation, HWID device admission, device-limit enforcement, published server snapshots.

The bot calls the mediator's admin API to create/update subscriptions and entitlements. The bot never decides whether a feed request may consume a device slot — that is always enforced by the mediator.

### Subscription delivery path
```
Happ client → GET /sub/{guid}/feed?token=...
  → ResolveUnifiedFeedDevice (HMAC-hashed HWID, atomic slot reservation)
  → DeviceAccessProfileService
    ↳ shared_catalog: returns current PublishedSnapshot
    ↳ managed: delegates to external provisioner via HTTP
```

Legacy per-device URLs (`/sub/{guid}/devices/{devicePublicId}/servers.txt`) remain valid for existing installs but new personal URLs cannot be created.

### VpnMediator internals (Program.cs)
All routes are in `Program.cs` (no controller classes). Key services:
- `SqliteMediatorRepository` — single SQLite repository; initialized on startup
- `CatalogRefreshWorker` / `ServerHealthWorker` — background services for upstream catalog and health probing
- `DeviceAccessRevocationWorker` — background revocation for managed-mode
- `HmacLinkSigner` — signs subscription and feed token links
- `AesGcmEndpointProtector` / `AesGcmDeviceCredentialProtector` — AES-GCM encryption at rest
- `ProbeAgentClient` — connects to VpnProbeAgent's Unix socket

Admin endpoints require `Authorization: Bearer <AdminToken>` and are protected by `AdminGuard.IsAllowed`. Subscription feed endpoints use per-subscription HMAC signed tokens.

### Device access modes
Configured via `VpnMediator__DeviceAccessMode`:
- `shared_catalog` (default) — all devices get the global published server snapshot; VLESS/Trojan/SS credentials cannot be revoked per-device
- `managed` — delegates profile creation/revocation to an external HTTP provider; can revoke per-device if the provider supports it

### Server health filtering
Off by default. Rollout path: `off` → `observe` → `enforce` → `confirmed_healthy`. Only `confirmed_healthy` publishes only quality-checked servers in the UI. Config under `VpnMediator__ServerHealth*` options.

### VpnAccessBot structure
- `vpn_access_bot/handlers/` — aiogram message/callback handlers
- `vpn_access_bot/telegram/` — Telegram-specific utilities
- `vpn_access_bot/mediator_client.py` — HTTP client for mediator admin API
- `vpn_access_bot/repositories.py` — SQLAlchemy async repository
- `vpn_access_bot/migrations.py` — SQLite schema migrations (applied at startup)
- `vpn_access_bot/config.py` — pydantic-settings configuration (env vars)

Multi-bot: configure multiple bots via `TELEGRAM_BOTS__0__*`, `TELEGRAM_BOTS__1__*`, etc. `DEFAULT_BOT_KEY` selects which bot generates subscription URLs and support references.

### Payment modes
- `manual` — admin uses `/approve_order ORDER_ID`
- `telegram_stars` — Telegram Stars invoices (currency `XTR`, integer amount)

### Key invariants
- HWID is never stored raw; only its HMAC is persisted (`DeviceIdentityHashKey`)
- Subscription feed responses are `Cache-Control: private, no-store`; reverse proxy must not cache them
- `shared_catalog` cannot revoke an already-imported credential; only `managed` mode with a cooperative provider can revoke upstream VPN access per-device
- Entitlement updates are versioned and idempotent; stale versions are rejected by the mediator

## Deployment

```bash
./update_vpn_mediator.sh --check-only  # preflight without deploying
./update_vpn_mediator.sh               # atomic three-service update with rollback
```

See `docs/deployment-guide.md` for production configuration requirements, and `deploy/mediator.env.example` / `deploy/bot.env.example` for required env vars.
