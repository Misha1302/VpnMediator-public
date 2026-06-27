# Happ capability and evidence policy

## Implemented safe flow

1. The user acts in a private Telegram chat.
2. The mediator creates or returns one independently revocable device credential.
3. The bot sends the URL in a no-preview message. `CopyTextButton` is used only within Telegram's length bound; a plain full URL is the fallback.
4. `HAPP_DEEP_LINK_TEMPLATE` is disabled by default. It may be enabled only after an official format and every advertised platform are verified.
5. The mediator marks first authenticated subscription fetch. The UI says “Happ received the subscription”; it does not claim the tunnel is connected.

Dynamic browser handoff has been retired. New onboarding creates a device credential directly and sends the URL in private Telegram chat. `GET /connect/{legacy-id}` is only a static `410 Gone` tombstone for previously sent links; it does not read a claim, create a token, redeem state or report activation.

## Response policy

Healthy responses contain real published servers only. Device usage and the support contact are delivered through Happ's official `announce` HTTP header as a Base64-encoded two-line message:

```text
📱 Подключено X из Y устройств
💬 Telegram: @RazaltushVpnBot
```

This metadata does not create fake VPN servers, cannot be selected as a connection endpoint, and does not participate in canonical catalog sorting or fingerprints. A temporary isolated compatibility status entry may be used only for blocking states until real-device HTTP/metadata behavior is proven.

## Required device matrix

Record Happ build and OS version for Android, iOS and Windows/macOS. Test add/copy, refresh, expiry, revoke, regenerate, reset, catalog unavailable, primary-domain outage/fallback and client metadata. Router/TV claims require named-device evidence.
