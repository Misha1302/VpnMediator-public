# Incident response

## General sequence

Contain, preserve financial state/evidence, rotate the affected secret, restore service, reconcile paid-but-not-applied orders, notify affected users when required, and document root cause.

## Token leak

Revoke/regenerate only the affected device; search sanitized access/audit records by safe IDs; never paste the URL into tickets.

## Admin/bot token leak

Rotate current token, configure previous token only with a short explicit expiry where continuity is required, audit previous-token use, then remove it. A bot token leak requires BotFather rotation and polling restart.

## Encryption key leak

Deploy a new key ID, retain the previous key only for migration, re-encrypt source endpoints through the local admin operation, regenerate exposed device credentials when confidentiality is uncertain, then remove previous material.

## Upstream compromise

Disable/revoke the source, keep last-known-good, inspect content fingerprint/anomaly evidence and require manual review before republishing.

## Paid but not applied

Do not refund or re-charge automatically. Keep `payment_received`, repair mediator availability, run idempotent reconciliation, verify one application, then communicate status.

## Host compromise

Isolate host, rotate all secrets, restore paired DBs to a clean host, verify migrations/catalog and force credential rotation as risk requires.
