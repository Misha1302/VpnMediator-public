# Device Issuance v2

> **Historical compatibility document.** New personal device-link issuance is disabled. The endpoint described below now returns `410 Gone` with `personal_device_links_disabled`; current connections use `POST /admin/subscriptions/{publicGuid}/feed-credential/ensure`. Existing public per-device URLs remain readable only to avoid breaking installed clients.

## Purpose

Device Issuance v2 separates the identity of an issuance operation from the user-visible device name. `displayName` and `requestedPlatform` are presentation metadata; a persisted onboarding `issuance_request_id` is the idempotency identity.

## Contract

The bot creates `onboarding_sessions.issuance_request_id` before calling the mediator and sends:

```http
POST /admin/subscriptions/{publicGuid}/device-tokens
Idempotency-Key: device-issuance:<issuance_request_id>
Content-Type: application/json
```

```json
{
  "displayName": "Happ · Android",
  "requestedPlatform": "android"
}
```

A new key returns `201` and `result: "created"`. Repeating the same key returns `200` and `result: "existing"` with the same credential. A different key can create another token even when platform and display name are identical.

The bot must not enable the v2 flow unless `/health/ready` reports `deviceIssuanceVersion >= 2`.

## Compatibility

Mediator schema 13 adds nullable `issuance_key` and `requested_platform` columns plus a partial unique index on `(subscription_id, issuance_key)`. Existing token IDs, secret hashes, revoked state and URLs are not changed.

Requests from an old bot without `Idempotency-Key` use a deterministic compatibility identity derived from the legacy display name. A hash-only legacy row is not revoked. At most one recoverable compatibility replacement is created for repeated legacy requests.

Bot schema 12 adds and backfills `onboarding_sessions.issuance_request_id`. The current bot writes only `device_public_id`. Reads retain a bounded fallback to the nullable legacy `handoff_claim_id` field and `waiting_activation` status for rows created by older bot versions; new sessions never dual-write the identifiers.

## Legacy credential recovery

A secret hash is not reversible. When a valid legacy feed request supplies the original secret, the mediator verifies the keyed hash and lazily stores an AES-GCM escrow in the same row. Invalid secrets never trigger backfill. If the encryption key is unavailable or ciphertext is corrupt, the API returns a typed recovery error; regeneration remains explicit.

## Invariants

- Display name and platform never define issuance identity.
- One persisted issuance request occupies at most one slot, including concurrent retries.
- Two different issuance requests may create two identical-platform devices.
- Regeneration transfers issuance identity to the replacement credential atomically.
- Active legacy credentials are never revoked by migration or startup.
- Public feed logs contain the path but not the query token.
