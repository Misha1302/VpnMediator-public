# Device feed binding

## Scope

Device feed binding protects the per-device subscription URL served by VpnMediator. It does **not** control VPN sessions after Happ has downloaded raw VLESS, Trojan, or Shadowsocks credentials from a shared upstream catalog.

The feature is intentionally conservative: it blocks a protected feed only when the same feed is requested from a confidently detected different platform. Network changes alone never block access.

## Compatibility model

Migration 18 adds policy data without changing public IDs, secret hashes, connection URLs, entitlements, or activation timestamps.

Existing rows are migrated as:

- `feed_policy_mode = legacy`
- `binding_state = grandfathered`

Legacy rows remain allowed even when the global mode is `enforce`. They can be migrated voluntarily through the device transfer operation. Regular renewal and regular link regeneration do not upgrade a legacy profile implicitly.

New rows use `DefaultNewDeviceFeedPolicy`. Keep it at `legacy` during the first deployment.

## Global modes

- `off`: no binding decisions and no observations. This is the immediate rollback switch.
- `observe`: record privacy-preserving observations and mismatch events, but never deny the feed.
- `enforce`: enforce only tokens whose own policy is `enforce`; legacy tokens remain grandfathered.

Recommended rollout:

1. Deploy migration 19 and the new code with `DeviceFeedBindingMode=off`, `DefaultNewDeviceFeedPolicy=legacy`, and `RequireDeviceIssuanceKey=false`.
2. Deploy every bot/client that creates devices and verify that each request sends a persisted `Idempotency-Key` plus `requestedPlatform`.
3. Review `legacy_issuance.*` and `protected_compatibility_issuance.*` audit events. Do not enable the issuance-key requirement while current clients still use the compatibility path.
4. Set `RequireDeviceIssuanceKey=true`. Missing keys are then rejected without occupying a device slot.
5. Set `DeviceFeedBindingMode=observe`, `DefaultNewDeviceFeedPolicy=enforce`, configure a dedicated `DeviceObservationHashKey`, and review Android, iOS, Windows, Linux, macOS, unknown-client, and network-change events for at least 7–14 days.
6. Set `DeviceFeedBindingMode=enforce` only after the observed client headers are reliable.
7. Offer existing users an explicit transfer/protection action. Do not silently migrate grandfathered tokens.

Binding rollback requires `DeviceFeedBindingMode=off`. API strictness rollback additionally requires `RequireDeviceIssuanceKey=false`. Neither rollback requires removing migrations 18 or 19.

## Privacy

VpnMediator never stores the full client IP or full User-Agent for this feature. IPv4 addresses are normalized to `/24`, IPv6 addresses to `/48`, and then HMAC-SHA256 is applied using `DeviceObservationHashKey`. The key must be independent from admin, token-signing, and encryption keys. Aggregated sightings and policy events are deleted after `DeviceFeedObservationRetentionDays` (14 days by default).

Network fingerprints are risk signals only. Wi-Fi/LTE transitions, CGNAT, roaming, and dynamic addresses make IP-based physical-device identity unreliable.

## Transfer operation

`POST /admin/subscriptions/{publicGuid}/device-tokens/{devicePublicId}/transfer` now returns `410 Gone`; device transfer is replaced by reuse of the shared subscription feed

Request:

```json
{
  "operationId": "device-transfer:<source-device-id>:android",
  "requestedPlatform": "android"
}
```

The operation is transactional and idempotent. It revokes the old feed token, creates an enforced unbound replacement for the selected platform, preserves the logical issuance identity when present, and stores the encrypted replacement credential for safe replay.

A server-side cooldown protects against repeated transfers. A failed transfer does not revoke the current token because the transaction is rolled back.

## Known limitation

Three Android devices using one downloaded raw upstream credential can still connect when the upstream provider exposes no credential-management API. Device feed binding raises friction against sharing the subscription URL; it is not strict physical-device enforcement.

## Issuance contract

Migration 19 adds `issuance_request_hash`. A stable issuance key is bound to the semantic request (subscription, requested display name, and requested platform). Replaying the same key with a different request returns `idempotency_key_reused` and never creates another slot.

SQL defaults remain `legacy/grandfathered` solely for old rows and interrupted deployments. Every new token is written with an explicit policy seed. Protected compatibility requests made while `RequireDeviceIssuanceKey=false` use a separate deterministic identity and cannot silently reuse a grandfathered credential.

Regeneration preserves the original issuance identity and policy. Transfer is the only operation that deliberately creates an `enforce/unbound` replacement for another platform.
