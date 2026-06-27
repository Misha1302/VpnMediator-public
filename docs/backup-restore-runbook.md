# Backup and restore runbook

## Backup

Install `deploy/backup.sh` as `/usr/local/libexec/vpn-backup.sh`, configure bot and mediator DB paths and enable `backup.timer`. The script uses SQLite `.backup`, runs `quick_check`, calculates SHA-256 and publishes the manifest last. Configure `BACKUP_AGE_RECIPIENT` for encrypted offsite copies.

## Restore drill

1. Stop bot and mediator writers.
2. Copy the latest complete manifest and referenced databases to an isolated host.
3. Set `BACKUP_AGE_IDENTITY_FILE` when encrypted.
4. Run `deploy/restore-drill.sh`.
5. Restore to staging paths, start services and verify migrations/readiness.
6. Test an existing device refresh and a revoked device denial.
7. Record RPO, RTO, checksums and reviewer.

The restore drill continues to understand older three-database manifests for compatibility, but new default backups contain only bot and mediator databases.
