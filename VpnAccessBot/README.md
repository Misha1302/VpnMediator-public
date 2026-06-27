# Razaltush VPN Access Bot

Telegram bot for selling and managing VPN subscriptions through `VpnMediator`.

The public product name is configured separately from technical package and service names.
The default brand is `Razaltush VPN`; internal identifiers remain `VpnAccessBot`, `VpnMediator`,
`vpn-access-bot.service` and `vpnmediator.service`.

## What it does

- Creates server-side purchase quotes for configurable device and period counts.
- Creates pending orders.
- Supports two payment modes:
  - `manual` — admin approves orders manually with `/approve_order ORDER_ID`.
  - `telegram_stars` — Telegram Stars invoice flow.
- Creates mediated VPN subscriptions through the idempotent mediator contract.
- Delivers one stable Happ subscription URL per subscription and reuses it on all devices.
- Shows subscription status.
- Lists HWID-backed devices and allows explicit disable or re-enable.
- Expires subscriptions through a background worker while preserving the distinction between natural expiration and administrative disablement.

## Architecture

```text
Telegram user
    ↓
Python bot
    ├── users / quotes / orders / applications / entitlements in SQLite
    ├── payment flow
    ├── user cabinet
    └── admin commands
    ↓ admin API
VpnMediator ASP.NET service
    ├── one encrypted subscription-feed credential per subscription
    ├── HMAC-only HWID device registry and atomic device-limit enforcement
    ├── compatibility per-device access tokens
    └── global published server snapshots
```

The bot owns business state: users, tariffs, orders, subscription lifetime.

`VpnMediator` owns technical subscription delivery: subscription-feed validation, HWID device admission/disablement, compatibility device-token validation, published server snapshots and entitlement enforcement. The bot never decides whether a feed request may consume a device slot.

## Setup

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install --require-hashes -r requirements.lock
pip install --no-deps -e .
cp .env.example .env
```

Edit `.env`.

Required values for the canonical public bot deployment:

```env
DEFAULT_BOT_KEY=razaltush
TELEGRAM_BOTS__0__KEY=razaltush
TELEGRAM_BOTS__0__TOKEN=<razaltush-bot-token>
TELEGRAM_BOTS__0__EXPECTED_USERNAME=RazaltushVpnBot
PRODUCT_NAME="Razaltush VPN"
ADMIN_TELEGRAM_IDS=123456789
MEDIATOR_BASE_URL=http://127.0.0.1:5062
MEDIATOR_ADMIN_TOKEN=<mediator-admin-token>
PUBLIC_SUBSCRIPTION_BASE_URL=http://192.168.1.71:5062
```

`TELEGRAM_BOT_TOKEN` remains a compatibility-only single-bot option. Do not configure it together
with indexed `TELEGRAM_BOTS__*` entries. A historical bot may be added as a non-public
compatibility channel, but `@RazaltushVpnBot` remains the only generated support identity.

Real secrets must come from environment variables, systemd environment files, or a secret manager.
Do not commit bot tokens, mediator admin tokens, upstream URLs, token hash keys, or production
systemd env files.

`PRODUCT_NAME` is used in the Telegram cabinet, onboarding, support screens and Stars invoices.
It must contain from 2 to 32 characters. The Telegram profile name and username are managed
separately through BotFather.

Manual payment mode is development-only unless a complete audited manual-payment procedure is implemented. Production validation requires Telegram Stars.

Then run:

```bash
python -m vpn_access_bot.main
```

## User onboarding flow

The user-facing flow is intentionally separated from internal VPN terminology:

```text
Активировать доступ → выбрать платформу → установить Happ → получить общую ссылку подписки → открыть её в Happ
```

A successful first subscription fetch is shown as **Подписка добавлена в Happ**. The bot does
not claim that the VPN tunnel is active because the current integration has no authoritative tunnel
health signal. Manual **Не появилось в Happ?** checking is a diagnostic fallback, not a required
happy-path step.

An unfinished order temporarily replaces ordinary purchase and trial calls to action, preventing
a user from starting a competing payment while the existing order is pending or being activated.

## Manual payment flow

1. User selects device count.
2. User selects period count.
3. Bot creates a quote and confirmation screen.
4. Bot creates a pending order.
5. Admin runs the approval command with the public order identifier shown by the bot.
6. Bot creates or extends the subscription and opens the device onboarding flow.

## Telegram Stars payment flow

Set:

```env
PAYMENT_MODE=telegram_stars
```

Then the bot sends Telegram Stars invoices and activates subscriptions after `successful_payment`.

Stars-specific rules implemented by the bot:

- invoice currency is `XTR`;
- provider token is an empty string;
- `TELEGRAM_PROVIDER_TOKEN` is not required;
- every invoice has exactly one price;
- `telegram_payment_charge_id` is saved for possible Stars refunds;
- activation happens only after Telegram sends `successful_payment`.

`PAYMENT_MODE=telegram_payments` is accepted as a deprecated alias and normalized to
`telegram_stars`.

Pricing is calculated server-side from configuration. For `XTR`, the amount is an integer number
of Stars, not cents or another minor unit. One product month is 30 days.

Before invoice creation and again during `pre_checkout_query`, the bot verifies mediator readiness,
server availability, order ownership, price, currency and pricing fingerprint. Checkout is rejected
when a working access cannot currently be issued.

## Connection UI

The primary user flow is:

```text
Главное меню → Открыть в Happ → one shared subscription URL
```

The bot does not ask the user to choose a device or create another link. Happ identifies each installation with `x-hwid`, and the mediator registers it automatically within the purchased limit. **Установить Happ** is a separate optional helper; its platform chooser is used only to select the official installation source.

## Device management

User flow:

```text
Главное меню → Мои устройства → выбранное устройство → отключить или подключить снова
```

The shared subscription URL is not regenerated during ordinary device management. Disabling a unified device preserves its HMAC identity and prevents that HWID from silently registering again. Re-enabling is allowed only when the subscription has a free slot.

The bot uses:

```http
GET  /admin/subscriptions/{publicGuid}/device-tokens
POST /admin/subscriptions/{publicGuid}/devices/{devicePublicId}/enable
DELETE /admin/subscriptions/{publicGuid}/device-tokens/{devicePublicId}
```

Legacy per-device-link callbacks from already sent Telegram messages are accepted, but they now return the stable shared subscription URL and never create, transfer or regenerate a personal credential. Existing public personal URLs are a mediator-side compatibility path only.

## Expiration model

The bot is the source of commercial truth. The mediator stores a versioned execution mirror.
Activation and renewal send the full desired state to:

```http
PUT /admin/subscriptions/{publicGuid}/entitlement
```

Stale versions cannot overwrite newer mediator state.

## Admin commands

```text
/admin
/approve_order ORDER_ID
/pending_orders
/failed_orders
/order ORDER_ID
/retry_order ORDER_ID
/refund_order ORDER_ID
/test_user_reset TELEGRAM_ID CONFIRM_RESET_TELEGRAM_ID
/sync_expired
/reconcile_status GUID
/reconcile_adopt_remote GUID REASON
/reconcile_adopt_expired GUID VERSION REASON
/reconcile_adopt_disabled GUID VERSION REASON
/reconcile_restore_local GUID REASON
/broadcast
/broadcast_regex REGEX
/broadcast_confirm TOKEN
```

Only Telegram IDs from `ADMIN_TELEGRAM_IDS` can use them.

Broadcast text starts on the next line and can contain multiple lines. Creating a campaign does not send anything immediately. The bot returns the exact recipient count, body length, SHA-256 digest and a short-lived confirmation command:

```text
/broadcast
Первая строка сообщения

Третья строка сообщения
```

`/broadcast_regex` applies a Python regular expression to the complete decimal Telegram ID (`fullmatch`, not a substring search):

```text
/broadcast_regex ^123\d+$
Сообщение только выбранным Telegram ID
```

The administrator must then send `/broadcast_confirm TOKEN` through the same bot and in the same private chat before the token expires. Broadcast commands are rejected in groups, even for administrators. The recipient set is snapshotted before confirmation, so the preview count and queued count cannot drift because a user registered between the two steps. In a multi-bot deployment, campaign identity and delivery ownership include the source bot key, preventing message-ID collisions between an administrator's private chats with different bots.

The message body is stored once in `broadcast_campaigns`; per-recipient outbox rows reference the campaign rather than duplicating the body. Recipient snapshotting and outbox population are committed in bounded batches. Reprocessing or concurrently confirming the same campaign is idempotent. Delivery is literal plain text, is retried with bounded exponential backoff, and becomes `terminal_failed` after the configured maximum attempts. Telegram permanent rejections and users who blocked every verified bot fail terminally immediately. Delivery remains at-least-once across a process crash after Telegram accepts a message but before the local commit. Completed campaign content and terminal outbox rows are removed by the non-financial retention job after the configured retention period.

`/order ORDER_ID` shows order and local subscription diagnostics without printing complete
device subscription URLs, bot tokens, admin tokens, or raw payment charge ids.

`/sync_expired` processes only new `active → expired` transitions. It does not repair
quarantined historical divergence. For a recognized legacy expiration, inspect the exact local
and remote snapshots with `/reconcile_status`, then use `/reconcile_adopt_expired` with the
observed remote version. Unknown remote `disabled` states are not adopted automatically. If provenance or independent operator evidence confirms a permanent revoke/refund, use `/reconcile_adopt_disabled GUID VERSION REASON`; otherwise keep the subscription quarantined.

`/refund_order ORDER_ID` is only for Telegram Stars orders with a saved
`telegram_payment_charge_id`. It calls Telegram Bot API `refundStarPayment` and marks the order
`refunded` only after Telegram accepts the refund.

For multi-bot payments, `origin_bot_key` records where the order started while
`payment_bot_key` records the bot that actually issued and accepted the Stars invoice. Pre-checkout,
payment evidence, financial notifications and refunds are bound to `payment_bot_key`; a second bot
cannot take ownership of an existing invoice. Background non-financial notifications prefer the
user's most recently active unblocked bot and persist the actual delivery channel.

`/test_user_reset` is a destructive test-only operation. It is disabled by default and requires both
`ALLOW_TEST_USER_RESET=true` and an explicit `TEST_USER_RESET_TELEGRAM_IDS` allowlist that is a
subset of `ADMIN_TELEGRAM_IDS`. The command first disables visible subscriptions through the normal
idempotent mediator entitlement operation, then archives local access, cancels pending test state,
removes the trial claim and advances a persisted test-reset epoch. Paid orders, payment evidence and
audit history are preserved, but only payment evidence created after the latest authorized reset
blocks a new trial for that allowlisted test user. Each reset epoch also gives the next trial a new
idempotency namespace, so retained entitlement history cannot absorb a repeated test activation.
Exact confirmation is required:

```text
/test_user_reset 123456789 CONFIRM_RESET_123456789
```

Turn `ALLOW_TEST_USER_RESET` back to `false` immediately after the test.

## User payment support

Users can send:

```text
/paysupport
```

The bot explains what to do if Stars were charged but activation did not finish, and includes the
latest relevant order number when available. It does not promise an automatic refund.

## Recovery

If payment was received but activation failed:

1. Run `/failed_orders`.
2. Inspect the order with `/order ORDER_ID`.
3. Check mediator health and logs.
4. Run `/retry_order ORDER_ID`.
5. Use `/refund_order ORDER_ID` only when activation cannot be completed and the order was paid
   through Telegram Stars.

The bot records payment first, commits a durable operation lease, calls the mediator outside the
SQLite write transaction and finalizes the local entitlement idempotently. Retrying uses the same
public order id and mediator idempotency key, so a paid order cannot be applied twice.

## systemd operations

Example environment file path:

```text
/etc/vpn-access-bot/bot.env
```

Example values with placeholders:

```env
DEFAULT_BOT_KEY=razaltush
TELEGRAM_BOTS__0__KEY=razakov
TELEGRAM_BOTS__0__TOKEN=<razakov-telegram-bot-token>
TELEGRAM_BOTS__1__KEY=razaltush
TELEGRAM_BOTS__1__TOKEN=<razaltush-telegram-bot-token>
ADMIN_TELEGRAM_IDS=<admin-telegram-id>
ALLOW_TEST_USER_RESET=false
TEST_USER_RESET_TELEGRAM_IDS=
PAYMENT_MODE=telegram_stars
DATABASE_URL=sqlite+aiosqlite:////var/lib/vpn-access-bot/vpn_bot.db
MEDIATOR_BASE_URL=http://127.0.0.1:5062
MEDIATOR_ADMIN_TOKEN=<mediator-admin-token>
PUBLIC_SUBSCRIPTION_BASE_URL=https://vpn.example
DEVICE_RESET_COOLDOWN_HOURS=12
EXPIRATION_CHECK_INTERVAL_SECONDS=1800
```

Do not publish this file or paste it into issues, logs, commits, or chat.

Common service commands:

```bash
sudo systemctl restart vpn-access-bot
sudo systemctl status vpn-access-bot --no-pager
journalctl -u vpn-access-bot -n 100 --no-pager
```

Mediator health check:

```bash
curl -fsS http://127.0.0.1:5062/ping
```

If Telegram connectivity is routed through Tor, basic checks:

```bash
systemctl status tor --no-pager
journalctl -u tor -n 100 --no-pager
```

Backups and restore verification:

```bash
sudo systemctl enable --now backup.timer
sudo BACKUP_DIR=/var/backups/vpn ../deploy/restore-drill.sh
```

The tracked backup script uses SQLite `.backup` for both databases and publishes a timestamp-matched
pair only after integrity checks.

## Production notes

Before real sales:

1. Put the bot and VpnMediator behind systemd/Docker Compose.
2. Use HTTPS for public subscription URLs.
3. Store secrets in environment variables or systemd secrets, not in Git.
4. Schedule backups for the bot DB and VpnMediator DB.
5. Configure source catalog in the mediator admin API.
6. Move from SQLite to PostgreSQL later only if operational load requires it.

## Development checks

```bash
python -m compileall vpn_access_bot
python -m ruff check .
python -m pytest
```
