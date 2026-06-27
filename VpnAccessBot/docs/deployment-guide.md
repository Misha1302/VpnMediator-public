# Deployment Guide

## Supported production shape

The current release supports one `VpnAccessBot` process and one `VpnMediator` process backed by
SQLite. Do not run multiple bot or mediator writer instances until a database-backed worker claim
or leader-election mechanism and a PostgreSQL migration have been introduced.

The browser and VPN clients reach only the public mediator routes. The Telegram bot reaches the
mediator administrative API over loopback or a private service network.

Multiple Telegram bot identities are hosted by the same `VpnAccessBot` process and share one domain
database. Configure them with indexed `TELEGRAM_BOTS__0__...` / `TELEGRAM_BOTS__1__...` variables.
Do not run one writer process per bot. Orders preserve both the originating channel and the bot that
actually issued the Stars invoice; refunds must use the latter.

## Pre-deploy validation

Run the complete release gate from the repository root:

```bash
./scripts/validate-release.sh
```

The gate requires Python, .NET SDK 10 and ShellCheck. It runs the architecture guard, Python tests,
.NET build/tests and shell validation. A release must not proceed when any stage is red.

## Services and least privilege

Use the templates in `deploy/`:

- `vpnmediator.service`;
- `vpn-access-bot.service`;
- `nginx.conf.example`;
- `backup.service` and `backup.timer`.

Create dedicated users and private configuration directories:

```bash
sudo useradd --system --home /var/lib/vpn-mediator --shell /usr/sbin/nologin vpn-mediator
sudo useradd --system --home /var/lib/vpn-access-bot --shell /usr/sbin/nologin vpn-access-bot
sudo install -d -m 0750 /etc/vpn-mediator /etc/vpn-access-bot
sudo install -m 0640 deploy/mediator.env.example /etc/vpn-mediator/mediator.env
sudo install -m 0640 deploy/bot.env.example /etc/vpn-access-bot/bot.env
```

Replace every placeholder. Production startup must reject example secrets, insecure HTTP public
URLs and invalid product options.

Generate independent mediator secrets:

```bash
openssl rand -base64 32   # VpnMediator__SourceEndpointProtectionKey
openssl rand -base64 48   # VpnMediator__DeviceTokenHashKey
openssl rand -base64 48   # VpnMediator__AdminToken
```

`VpnMediator__LinkSigningSecret` stays empty unless an explicitly approved legacy-link migration
window is active.

## Product configuration

The bot uses lists, not unrelated min/max values:

```dotenv
PURCHASABLE_PERIOD_OPTIONS=1,3,6,12
PURCHASABLE_DEVICE_OPTIONS=1,2,3,4,5,6,7,8,9,10,11,12
PRICING_DURATION_DISCOUNTS=1:0,3:10,6:20,12:30
```

The application validates that options are positive, sorted and unique and that discount periods
belong to the configured catalog. Quotes and orders store a deterministic pricing fingerprint, so
a stale unpaid quote cannot silently use a changed catalog.

## Health and payment readiness

- Liveness: `GET /health/live`.
- Readiness: `GET /health/ready`.

A new trial, invoice, Telegram pre-checkout confirmation or manual approval is permitted only when:

- the mediator responds successfully;
- the schema migration count is current;
- the service is ready;
- the published catalog contains at least one real server;
- the catalog is fresh or a non-empty last-known-good snapshot is available.

A temporary readiness failure never hides an existing active subscription and never discards an
already received payment. Such an order remains retryable.

## Reverse proxy and rate limits

Start from `deploy/nginx.conf.example`. Expose only:

- `/sub/`;
- `/connect/` only as a static `410 Gone` compatibility tombstone;
- `/health/live`;
- `/health/ready`.

Never expose `/admin/`. The application trusts forwarded headers only from the loopback reverse
proxy. If the network topology changes, explicitly update the trusted proxy list rather than
accepting arbitrary forwarded addresses.

The Nginx and application rate limits intentionally overlap:

- subscription fetches: 120 requests/minute per client IP;
- legacy `/connect/` tombstone: 15/minute.

There are no public handoff redeem or status endpoints in the current runtime.

Subscription URLs contain bearer material. Access logs for `/sub/` must be disabled or sanitized, must never contain `$request_uri`, and both the application and Nginx must disable caching. `/connect/` is retained only as a no-store tombstone for old links.

## Admin-token rotation

The mediator supports a short two-token rotation window:

1. Generate a new token.
2. Put the old token in `VpnMediator__PreviousAdminToken` and the new token in
   `VpnMediator__AdminToken`.
3. Restart the mediator.
4. Update `MEDIATOR_ADMIN_TOKEN` for the bot and restart the bot.
5. Verify administrative calls.
6. Clear `VpnMediator__PreviousAdminToken` and restart the mediator.

Do not keep the previous token indefinitely.

## Correlation IDs and logs

Nginx sends `X-Correlation-ID`; the mediator validates or generates it and returns it in the
response. The bot propagates the current operation correlation ID to mediator requests. Logs may
contain operation names, public order IDs, public subscription GUIDs, retry counts and stable error
codes. They must not contain raw device secrets, complete handoff URLs, upstream credentials,
Telegram bot tokens or mediator admin tokens.

## Backups

Install the verified backup script and timer:

```bash
sudo install -D -m 0750 deploy/backup.sh /usr/local/libexec/vpn-backup.sh
sudo systemctl enable --now backup.timer
```

Backups use SQLite `.backup`, publish the bot and mediator files with one timestamp and run
`PRAGMA quick_check` before publication. Before a schema upgrade, stop the services and create an
additional pre-upgrade backup.

## Restore drill

A backup is not considered verified until it passes the tracked restore drill:

```bash
sudo BACKUP_DIR=/var/backups/vpn ./deploy/restore-drill.sh
```

The drill restores a timestamp-matched pair to a temporary directory, runs `quick_check` and
`foreign_key_check`, verifies required tables and migration versions, and prints critical row
counts. Run it after every deployment that changes migrations and on a scheduled basis.

## Operational alerts

Configure the process supervisor or log platform to alert on:

- payment succeeded followed by activation failure;
- `/health/ready` returning non-ready or `serverCount == 0`;
- repeated catalog refresh failures;
- stale `WorkerHealth` records;
- backup or restore-drill failure;
- elevated HTTP 5xx/429 rates;
- repeated invalid handoff or device-token attempts.

The bot sends idempotent Telegram alerts to configured administrators for mediator readiness
failures, paid orders awaiting recovery and stale background workers. Use
`/product_funnel [DAYS]` for the built-in funnel snapshot. Backup failures, HTTP-rate anomalies and
infrastructure-level signals still require the deployment monitoring platform.

## Post-deploy smoke checks

```bash
curl -fsS http://127.0.0.1:5062/health/live
curl -i http://127.0.0.1:5062/health/ready
curl -i https://vpn.example/health/ready
```

Then run the trial, paid purchase, renewal, device-limit upgrade and failure/recovery scenarios from
`docs/release-checklist.md`. Never paste complete subscription or handoff URLs into tickets,
chat, logs or screenshots.

## Test-user reset safety

`/test_user_reset` exists only for controlled end-to-end purchase testing. Production defaults must
remain:

```dotenv
ALLOW_TEST_USER_RESET=false
TEST_USER_RESET_TELEGRAM_IDS=
```

To run a test, temporarily set the flag to `true` and allowlist only the exact administrator-owned
test Telegram ID. The target must also be in `ADMIN_TELEGRAM_IDS`. Back up both SQLite databases,
verify mediator readiness, then issue the command with the exact confirmation token. The operation
is idempotent per Telegram message, disables access through the mediator before local archival and
preserves paid orders, payment inbox rows and audit events. It advances a test-only reset epoch so
pre-reset payment history does not block the next trial and the next trial receives a distinct
idempotency identity. Any payment accepted after the reset blocks trial again. Disable the flag
immediately afterward. Never implement this workflow with direct SQL deletion of a user.
