# Support Guide

## User-facing categories

- VPN does not connect.
- Happ cannot add the VPN.
- All device places are occupied.
- Payment succeeded but access was not applied.
- Renewal or device-limit upgrade failed.
- Other issue.

The bot must always state whether the current access and payment are safe and provide one concrete
next action. Internal exception text, HTTP payloads and database status names are never sent to the
user or Telegram administrator.

## Allowed diagnostic data

- Telegram ID and username;
- public order ID;
- public subscription GUID for administrators;
- localized subscription/order state;
- validity date;
- active/max device counts;
- stable error code;
- timestamp and correlation ID.

## Never include

- raw device token or legacy claim secret;
- complete subscription or legacy handoff URL;
- upstream URL or credentials;
- Telegram bot token;
- mediator admin token;
- full payment charge ID;
- stack trace in Telegram.

## Payment succeeded but activation failed

1. Find the order by public order ID.
2. Verify that the provider payment identifier and paid timestamp are present.
3. Check `/health/ready`; do not retry while the catalog is empty or migrations are incomplete.
4. Use the exact-order retry action. Never create a replacement order and never edit the paid
   amount.
5. Confirm that entitlement, segment and adjustment rows were applied once.
6. Reply with the correlation ID, not the exception text.
7. If activation cannot be restored, follow the operator refund runbook.

An existing active subscription must remain visible and usable until its prior expiry even while a
renewal retry is pending.

## Trial issue

The eligibility result distinguishes already used, paid history, active access, activation in
progress, failed activation retry and service unavailability. A mediator outage does not consume the
trial. A failed activation retries the same claim and idempotency key; support must not create a
second claim manually.

## Device place issue

The authoritative active/max counts come from the mediator. Ask the user either to revoke a named
old device or buy a larger limit. Do not reset every device unless the user explicitly chooses that
action.

## Device-link issue

First verify ownership and authoritative device state. Reopening the link screen must return the same active credential; do not regenerate unless the user explicitly confirms that existing Happ refreshes will stop. If the primary domain is unavailable, follow the documented fallback-domain procedure. Never ask the user to paste the URL into support. Legacy handoff links are supported only during the migration window.

## Support request routing

When `SUPPORT_CHAT_ID` is configured, the bot persists the request and copies the user's supported
message to the support chat. An administrator reply is routed only through the mapped request. If
delivery fails, the bot says that support is temporarily unavailable and does not falsely claim
that the request was received.
