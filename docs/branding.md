# Razaltush VPN branding

The public brand is configured independently from technical project identifiers.
Do not rename `VpnMediator`, `VpnAccessBot`, systemd units, database files or API routes only to
change the product name.

## Runtime configuration

Telegram bot environment:

```env
PRODUCT_NAME="Razaltush VPN"
```

Mediator environment:

```env
VpnMediator__ProductName="Razaltush VPN"
VpnMediator__SupportTelegramBotUsername=@RazaltushVpnBot
```

The Telegram username is also shown in Happ's subscription announcement together with the current device usage.

The name must contain from 2 to 32 characters. It is displayed in:

- the Telegram home screen and cabinet;
- onboarding and support screens;
- Telegram Stars invoices;
- the browser handoff page;
- activation confirmation messages.

After changing production environment files, restart both services:

```bash
sudo systemctl daemon-reload
sudo systemctl restart vpnmediator.service vpn-access-bot.service
```

## Telegram profile

The visible Telegram profile is managed separately through BotFather:

1. `/setname` → `Razaltush VPN`;
2. `/setdescription` → product description;
3. `/setabouttext` → short product description;
4. `/setusername` → a free username ending in `bot`, for example `RazaltushVpnBot`.

Changing the Telegram username does not change the bot token, but old public links using the
previous username should be updated. `VpnMediator__SupportTelegramBotUsername` owns the username
shown in Happ metadata and legacy-link recovery pages. The old bot may remain configured as a
compatibility channel for existing Telegram users, but it must not remain the public support target.
