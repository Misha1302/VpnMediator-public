# Credential lifecycle

## Unified feed

Each subscription has one stable URL: `/sub/{publicGuid}/feed?token=...`. The Bot obtains it
through `POST /admin/subscriptions/{publicGuid}/feed-credential/ensure`. Happ supplies
`x-hwid`; Mediator stores only a dedicated-key HMAC and atomically creates or reuses a device
row under the purchased limit. Repeated delivery of the URL is idempotent and does not create a
device until a valid HWID requests the feed.

Disabling a unified device preserves its identity and blocks later feed refreshes. Re-enable is
explicit and requires a free slot. A fetch proves only that Happ requested the feed, not that a
VPN tunnel was established.

## Compatibility

New personal URLs are not issued, redisplayed, regenerated or transferred. The former admin
issuance endpoints are absent. Old public per-device URLs remain readable during the compatibility
window and continue to count against the same entitlement. Old Telegram callbacks resolve to the
stable shared feed without mutating a personal credential.

Readiness advertises `unifiedSubscriptionFeedEnabled=true`,
`sharedSubscriptionLinksOnly=true` and `personalDeviceLinksEnabled=false`.

