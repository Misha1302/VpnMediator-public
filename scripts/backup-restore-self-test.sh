#!/usr/bin/env bash

set -Eeuo pipefail
umask 077

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
temporary_directory="$(mktemp -d /tmp/vpn-backup-self-test.XXXXXX)"
cleanup()
{
    rm -rf -- "$temporary_directory"
}
trap cleanup EXIT

mkdir -p "$temporary_directory/bin" "$temporary_directory/state" \
    "$temporary_directory/backups"
cat > "$temporary_directory/bin/systemctl" <<'SYSTEMCTL'
#!/usr/bin/env bash
set -Eeuo pipefail
state_dir="${FAKE_SYSTEMD_STATE_DIR:?}"
command_name="$1"
shift
case "$command_name" in
    is-active)
        [[ "${1:-}" == "--quiet" ]] && shift
        [[ -f "$state_dir/$1.active" ]]
        ;;
    stop)
        rm -f -- "$state_dir/$1.active"
        printf 'stop %s\n' "$1" >> "$state_dir/actions"
        ;;
    start)
        : > "$state_dir/$1.active"
        printf 'start %s\n' "$1" >> "$state_dir/actions"
        ;;
    *)
        exit 2
        ;;
esac
SYSTEMCTL
chmod 0755 "$temporary_directory/bin/systemctl"

touch "$temporary_directory/state/vpn-access-bot.service.active"
touch "$temporary_directory/state/vpnmediator.service.active"

bot_database="$temporary_directory/bot.db"
mediator_database="$temporary_directory/mediator.db"
for version in $(seq 1 34); do
    sqlite3 "$bot_database" \
        "CREATE TABLE IF NOT EXISTS schema_migrations(version INTEGER PRIMARY KEY); INSERT INTO schema_migrations VALUES($version);"
done
for table_name in users orders subscriptions trial_claims onboarding_sessions \
    access_operation_leases product_events telegram_update_inbox commerce_policy \
    refund_plans capacity_state_transitions; do
    sqlite3 "$bot_database" "CREATE TABLE $table_name(id INTEGER);"
done
for version in $(seq 1 24); do
    sqlite3 "$mediator_database" \
        "CREATE TABLE IF NOT EXISTS mediator_migrations(version INTEGER PRIMARY KEY); INSERT INTO mediator_migrations VALUES($version);"
done
for table_name in mediated_subscriptions device_access_tokens connection_handoff_claims \
    device_access_profiles server_health_states server_probe_observations; do
    sqlite3 "$mediator_database" "CREATE TABLE $table_name(id INTEGER);"
done

PATH="$temporary_directory/bin:$PATH" \
FAKE_SYSTEMD_STATE_DIR="$temporary_directory/state" \
BACKUP_DIR="$temporary_directory/backups" \
BOT_DATABASE_PATH="$bot_database" \
MEDIATOR_DATABASE_PATH="$mediator_database" \
BACKUP_RETENTION_DAYS=14 \
    "$ROOT/deploy/backup.sh" >/dev/null

expected_actions=$'stop vpn-access-bot.service\nstop vpnmediator.service\nstart vpnmediator.service\nstart vpn-access-bot.service'
actual_actions="$(cat "$temporary_directory/state/actions")"
if [[ "$actual_actions" != "$expected_actions" ]]; then
    printf 'Unexpected coordinated-backup service lifecycle:\n%s\n' "$actual_actions" >&2
    exit 1
fi

EXPECTED_BOT_MIGRATION=34 \
EXPECTED_MEDIATOR_MIGRATION=24 \
BACKUP_DIR="$temporary_directory/backups" \
    "$ROOT/deploy/restore-drill.sh" >/dev/null

# A source database with a migration gap must not receive a false restore PASS even when
# its manifest and checksums faithfully describe that broken snapshot.
rm -rf -- "$temporary_directory/backups"
mkdir -p "$temporary_directory/backups"
sqlite3 "$bot_database" 'DELETE FROM schema_migrations WHERE version = 17;'
PATH="$temporary_directory/bin:$PATH" \
FAKE_SYSTEMD_STATE_DIR="$temporary_directory/state" \
BACKUP_DIR="$temporary_directory/backups" \
BOT_DATABASE_PATH="$bot_database" \
MEDIATOR_DATABASE_PATH="$mediator_database" \
BACKUP_RETENTION_DAYS=14 \
    "$ROOT/deploy/backup.sh" >/dev/null
if EXPECTED_BOT_MIGRATION=34 \
    EXPECTED_MEDIATOR_MIGRATION=24 \
    BACKUP_DIR="$temporary_directory/backups" \
    "$ROOT/deploy/restore-drill.sh" >/dev/null 2>&1; then
    printf 'Restore drill accepted a database with a migration-history gap.\n' >&2
    exit 1
fi

printf 'Coordinated backup and strict restore self-test passed.\n'
