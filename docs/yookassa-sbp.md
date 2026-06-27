# YooKassa SBP checkout

## Status

The implementation is disabled by default. Telegram Stars remain available. Enabling the
external button is an explicit product-policy decision because Telegram requires Stars for
digital goods sold inside bots.

## User flow

1. The bot creates one immutable purchase quote.
2. The user selects either Telegram Stars or the SBP URL button.
3. The first provider selection atomically consumes the quote. The other path becomes stale.
4. The checkout page creates a YooKassa payment only after an explicit POST.
5. Access is granted only after a webhook, an authenticated API read, durable inbox persistence,
   reconciliation, and the existing entitlement application worker.

The checkout states that the payment is one-time and has no automatic renewal.

## Configuration

Keep `EXTERNAL_PAYMENT_ENABLED=false` until all values and the reverse proxy are verified.

```dotenv
PAYMENT_MODE=telegram_stars
YOOKASSA_INTEGRATION_ENABLED=false
EXTERNAL_PAYMENT_ENABLED=false
PRICING_BASE_DEVICE_MONTH_RUB_KOPECKS=19900
CHECKOUT_PUBLIC_BASE_URL=https://pay.example.com
CHECKOUT_BIND_HOST=127.0.0.1
CHECKOUT_BIND_PORT=8082
CHECKOUT_TOKEN_SECRET=<at-least-32-random-bytes>
YOOKASSA_SHOP_ID=<shop-id>
YOOKASSA_SECRET_KEY=<secret-key>
YOOKASSA_RETURN_URL=https://pay.example.com/payment/return
YOOKASSA_WEBHOOK_PATH_SECRET=<at-least-24-random-characters>
```

Configure YooKassa to send `payment.succeeded` and `payment.canceled` to:

`https://pay.example.com/webhooks/yookassa/<YOOKASSA_WEBHOOK_PATH_SECRET>`

Use `deploy/nginx-checkout.conf.example` as the reverse-proxy baseline. Do not log checkout or
webhook URLs. Keep port 8082 bound to loopback.

## Security and recovery

- Checkout URLs contain a signed, expiring quote capability and no Telegram ID.
- GET does not consume a quote or contact YooKassa, so link previews have no financial effect.
- Payment creation uses a stable idempotence key derived from the public order ID.
- The webhook body is not trusted. The service reads the payment from YooKassa using shop
  credentials and checks order ID, amount, currency, paid flag, and status.
- Provider evidence is committed to `payment_inbox` before entitlement logic runs.
- Duplicate notifications are deduplicated by `(provider, provider_charge_id)`.
- Evidence mismatch is retained as `manual_review`; the user is told not to pay again.
- A second provider cannot create another order for the same quote.

The current policy does not perform automatic refunds. Any verified duplicate or mismatched
payment must be reviewed through the existing refund and operator workflow.

## Self-employed receipt gate

Before production enablement, confirm in the YooKassa account which fiscalization mode is active.
Complete a real low-value canary and verify that the receipt appears in the configured
self-employed receipt flow. A successful API payment alone does not prove fiscal compliance.

## Canary

1. Back up the bot SQLite database and verify restore.
2. Deploy with `EXTERNAL_PAYMENT_ENABLED=false` and run migration 34.
3. Configure DNS, TLS, Nginx, YooKassa credentials, and webhook.
4. Use the YooKassa test shop to exercise pending, succeeded, canceled, duplicate webhook,
   amount mismatch, process restart, and return-before-webhook.
5. Enable the feature for a controlled production window.
6. Complete one low-value SBP payment and verify the receipt, inbox row, order activation,
   Telegram notification, and restart recovery.
7. Set `EXTERNAL_PAYMENT_ENABLED=false` immediately if manual-review volume, webhook lag,
   receipt generation, or activation differs from the expected state.

Disabling `EXTERNAL_PAYMENT_ENABLED` removes the SBP button. Keep
`YOOKASSA_INTEGRATION_ENABLED=true` until every existing provider payment is terminal so webhook
and reconciliation recovery remain available. Do not remove schema migration 34 during rollback.
