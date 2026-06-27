# External evidence required

1. Current networked `pip-audit` and NuGet advisory checks.
2. Real Telegram token, Stars payment, retries, cancellation and refund.
3. Happ lifecycle on advertised platforms: add, first fetch, refresh, revoke, regenerate and expiration.
4. Proof that `/admin/*` is externally unreachable while device feed routes work with no-store behavior.
5. DNS/TLS renewal, NTP, monitoring and alert delivery.
6. Production-like load, SQLite contention and backup duration.
7. Encrypted offsite two-database backup and clean-host restore with measured RPO/RTO.
8. Five-person novice usability test and canary rollback observation.
9. Confirmation that product/support text does not claim upstream-level physical-device enforcement in `shared_catalog`.
10. If `managed` is later enabled: real provider create/update/revoke, credential uniqueness and concurrent-session enforcement.
