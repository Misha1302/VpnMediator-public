# User guide

## First connection

1. Activate the trial or buy a subscription.
2. Press **Открыть в Happ**.
3. Add the displayed subscription URL to Happ.
4. If Happ is not installed yet, use **Установить Happ** and choose a platform only to open the correct official installation source.

The same subscription URL is used on every device. The user does not create or select a separate link for each device. Happ sends its device identifier when it refreshes the subscription, and the mediator counts distinct devices against the purchased limit.

A successful refresh is shown as **Подписка добавлена в Happ**. This confirms that Happ fetched the subscription; it does not prove that the VPN tunnel is currently active.

## Main actions

- **Открыть в Happ** — open the one shared subscription URL. No device or platform selection is required.
- **Установить Happ** — optional installation helper; platform selection affects only the installation instructions.
- **Мои устройства** — view HWID-backed devices registered automatically through the shared URL.
- **Отключить устройство** — block later subscription refreshes for that device and free the slot after the operation completes.
- **Подключить снова** — re-enable the same device when a slot is available.
- **Продлить доступ** — choose a new duration without changing the current device limit.

The current interface does not expose per-device link generation, link regeneration or transfer. Old Telegram buttons created by earlier releases are accepted, but they now deliver the same shared subscription URL without mutating any personal credential.

## Device limit

Opening the shared URL on a known device reuses its existing slot. A new HWID receives a slot only when the number of occupied legacy and unified devices is below the paid limit. When the limit is reached, open **Мои устройства** and disable an unused device before connecting another one.

## Shared-catalog limitation

Disabling a device blocks later subscription responses for its HWID. A raw VPN credential already downloaded from the shared upstream catalog may continue working independently of the mediator.
