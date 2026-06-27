# Security model

## Trust boundaries

Telegram actor identity comes only from Telegram's authenticated update actor. The bot owns pricing, orders, payment state and trials. The mediator owns entitlement mirroring, device-token issuance, catalog refresh and subscription enforcement. The external `subscription_url` owns the actual VPN credentials and VPN-node behavior.

## Device boundary

Each paid slot receives a high-entropy device token. Only its hash is used for validation; redisplay escrow is AES-GCM encrypted. Token creation, first-fetch transition, regeneration and revocation are transactional and entitlement-scoped. Legacy non-device-scoped subscription links remain disabled.

New policy-version-2 tokens can bind to a keyed HMAC of Happ's `x-hwid`. Raw HWID values are not
stored or logged. Existing policy-version-1 tokens retain their previous platform-heuristic
semantics until an explicit transfer creates a replacement. IP addresses and User-Agent strings
remain diagnostic/risk signals rather than identity.

In `shared_catalog`, the security boundary ends after the mediator returns the upstream catalog. Raw credentials extracted from that response are controlled by the upstream, not by the mediator. The system does not claim otherwise.

## Secrets

Admin, device-token HMAC, credential-encryption and source-endpoint-encryption keys are separate. Raw subscription URLs, VPN credentials and authentication tokens are excluded from logs, callbacks and analytics. Every `/sub/*` response is application-enforced as `private, no-store` with no-referrer/noindex headers, and Nginx bypasses proxy caching for the same route. Environment files and backup material must remain root-readable only.

## Network and storage

The mediator binds loopback behind an allowlisted reverse proxy. Forwarded headers are trusted only from loopback proxies. SQLite uses WAL, foreign keys, busy timeout and a single writer. Backups use SQLite `.backup`, integrity checks and a checksum manifest covering bot and mediator databases.

## Residual risks

Telegram history may retain a feed URL received by a user. Revocation blocks later feed access but cannot revoke a raw upstream credential copied from an earlier response.

## Retired browser handoff

The dynamic `/connect/*` claim/redeem/status flow is removed. The only compatibility route is `GET /connect/{legacy-id}`, which returns a static `410 Gone` page and never reads or mutates credential state. New Telegram keyboards emit `credential:*` callbacks; old `handoff:*` callback identifiers are accepted for one transition release only so already sent buttons do not break.
