# Multiple Telegram bots

## Architecture

`@RazaltushVpnBot` is the primary public channel. `@RazakovVpnBot` may remain enabled only as a compatibility channel for existing users; both are presentation channels of one product. One Python process owns one Dispatcher, one worker set, one SQLite database, and multiple verified aiogram `Bot` instances.

A Telegram user remains one `users` row. Trial history, subscriptions, discounts, referrals, devices, and commercial entitlement are global to that user. Channel-specific facts are stored in `telegram_bot_channels`, `user_bot_channels`, and nullable `origin_bot_key` / `delivery_bot_key` fields introduced by bot schema 13.

## Configuration

Use indexed environment variables shown in `deploy/bot.env.example`. On startup each enabled definition is checked with Telegram `getMe`; the stable `bot_key` is the internal identity, while username is verified metadata. `TELEGRAM_BOT_TOKEN` remains a compatibility path for one bot.

## Routing rules

Orders, invoices, refunds, onboarding, support, and durable notifications prefer the operation's origin bot. Telegram-local identifiers are namespaced by bot key. Workers run once and use `NotificationSender`; they do not receive an arbitrary global bot.

A required bot that fails startup verification makes readiness fail. An optional bot may degrade while another channel remains operational. Real fallback behavior must be verified against Telegram errors before production rollout.
