# Known limitations

- Device limits are enforced at the unified Happ feed by HMAC-hashed HWID.
- Disabling a device blocks future feed refreshes but cannot revoke an already downloaded raw
  upstream credential.
- A subscription fetch does not prove that a VPN tunnel is established.
- Server reachability and latency are not measured; fresh means successfully fetched and
  syntactically validated.
- SQLite supports one writer process per service.
- Telegram updates are at-least-once; domain idempotency remains mandatory.
- Real Telegram Stars, Happ, DNS/TLS, systemd, monitoring and offsite restore require production
  verification.

