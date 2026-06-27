#!/usr/bin/env bash

set -Eeuo pipefail
umask 077

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
BACKUP_DIR="${BACKUP_DIR:-/var/backups/vpn}"
RELEASE_METADATA_FILE="${RELEASE_METADATA_FILE:-$SCRIPT_DIR/../release/build-metadata.txt}"
EXPECTED_BOT_MIGRATION="${EXPECTED_BOT_MIGRATION:-}"
EXPECTED_MEDIATOR_MIGRATION="${EXPECTED_MEDIATOR_MIGRATION:-}"
EXPECTED_PROVIDER_MIGRATION="${EXPECTED_PROVIDER_MIGRATION:-1}"
BACKUP_AGE_IDENTITY_FILE="${BACKUP_AGE_IDENTITY_FILE:-}"
ALLOW_LEGACY_BACKUP_MANIFEST="${ALLOW_LEGACY_BACKUP_MANIFEST:-false}"

read_release_metadata()
{
    local key="$1"
    [[ -f "$RELEASE_METADATA_FILE" ]] || return 1
    awk -F= -v key="$key" '$1 == key { print substr($0, index($0, "=") + 1) }' \
        "$RELEASE_METADATA_FILE"
}

if [[ -z "$EXPECTED_BOT_MIGRATION" ]]; then
    EXPECTED_BOT_MIGRATION="$(read_release_metadata bot_schema_version || true)"
fi
if [[ -z "$EXPECTED_MEDIATOR_MIGRATION" ]]; then
    EXPECTED_MEDIATOR_MIGRATION="$(read_release_metadata mediator_schema_version || true)"
fi
if [[ ! "$EXPECTED_BOT_MIGRATION" =~ ^[1-9][0-9]*$ || \
      ! "$EXPECTED_MEDIATOR_MIGRATION" =~ ^[1-9][0-9]*$ ]]; then
    printf 'Expected schema versions are missing or invalid. Set EXPECTED_*_MIGRATION or provide %s\n' \
        "$RELEASE_METADATA_FILE" >&2
    exit 1
fi

for command_name in sqlite3 sha256sum; do
    command -v "$command_name" >/dev/null 2>&1 || {
        printf '%s is required\n' "$command_name" >&2
        exit 1
    }
done

latest_manifest="$(find "$BACKUP_DIR" -maxdepth 1 -type f \
    \( -name 'vpn_bundle.*.manifest' -o -name 'vpn_pair.*.manifest' \) \
    -printf '%f\n' | sort | tail -n 1)"
if [[ -z "$latest_manifest" ]]; then
    printf 'No complete backup manifest found in %s\n' "$BACKUP_DIR" >&2
    exit 1
fi
manifest_path="$BACKUP_DIR/$latest_manifest"

read_manifest_value()
{
    local key="$1"
    awk -F= -v key="$key" '$1 == key { print substr($0, index($0, "=") + 1) }' \
        "$manifest_path"
}

format_version="$(read_manifest_value format_version)"
timestamp="$(read_manifest_value timestamp_utc)"
bot_file="$(read_manifest_value bot_file)"
mediator_file="$(read_manifest_value mediator_file)"
provider_file="$(read_manifest_value provider_file)"
bot_checksum="$(read_manifest_value bot_sha256)"
mediator_checksum="$(read_manifest_value mediator_sha256)"
provider_checksum="$(read_manifest_value provider_sha256)"
encrypted="$(read_manifest_value encrypted)"
snapshot_mode="$(read_manifest_value snapshot_mode)"
bot_schema_versions_manifest="$(read_manifest_value bot_schema_versions)"
mediator_schema_versions_manifest="$(read_manifest_value mediator_schema_versions)"
bot_schema_fingerprint_manifest="$(read_manifest_value bot_schema_fingerprint)"
mediator_schema_fingerprint_manifest="$(read_manifest_value mediator_schema_fingerprint)"

if [[ "$format_version" != "1" && "$format_version" != "2" && "$format_version" != "3" ]]; then
    printf 'Unsupported backup manifest version: %s\n' "$format_version" >&2
    exit 1
fi
if [[ -z "$timestamp" || -z "$bot_file" || -z "$mediator_file" || \
      -z "$bot_checksum" || -z "$mediator_checksum" ]]; then
    printf 'Backup manifest is incomplete: %s\n' "$manifest_path" >&2
    exit 1
fi
if [[ "$format_version" == "2" && \
      ( -z "$provider_file" || -z "$provider_checksum" ) ]]; then
    printf 'Provider backup fields are missing from version 2 manifest\n' >&2
    exit 1
fi
if [[ "$format_version" != "3" && "$ALLOW_LEGACY_BACKUP_MANIFEST" != "true" ]]; then
    printf 'Legacy backup manifest version %s lacks coordinated-snapshot evidence; refusing false PASS\n' \
        "$format_version" >&2
    exit 1
fi
if [[ "$format_version" == "3" ]]; then
    if [[ "$snapshot_mode" != "systemd_quiesced" || \
          -z "$bot_schema_versions_manifest" || \
          -z "$mediator_schema_versions_manifest" || \
          -z "$bot_schema_fingerprint_manifest" || \
          -z "$mediator_schema_fingerprint_manifest" ]]; then
        printf 'Version 3 backup manifest lacks coordinated snapshot or migration-set evidence\n' >&2
        exit 1
    fi
fi

backup_files=("$bot_file" "$mediator_file")
if [[ "$format_version" == "2" ]]; then
    backup_files+=("$provider_file")
fi
for backup_file in "${backup_files[@]}"; do
    if [[ ! -f "$BACKUP_DIR/$backup_file" ]]; then
        printf 'Manifest references missing backup: %s\n' "$backup_file" >&2
        exit 1
    fi
done

temporary_directory="$(mktemp -d)"
cleanup()
{
    rm -rf -- "$temporary_directory"
}
trap cleanup EXIT

restore_file()
{
    local source_name="$1"
    local destination="$2"

    if [[ "$encrypted" == "true" ]]; then
        command -v age >/dev/null 2>&1 || {
            printf 'age is required to restore this encrypted backup\n' >&2
            exit 1
        }
        if [[ -z "$BACKUP_AGE_IDENTITY_FILE" ]]; then
            printf 'BACKUP_AGE_IDENTITY_FILE is required for encrypted backups\n' >&2
            exit 1
        fi
        age -d -i "$BACKUP_AGE_IDENTITY_FILE" \
            -o "$destination" "$BACKUP_DIR/$source_name"
    else
        cp -- "$BACKUP_DIR/$source_name" "$destination"
    fi
}

bot_restore="$temporary_directory/vpn_bot.db"
mediator_restore="$temporary_directory/vpn_mediator.db"
provider_restore="$temporary_directory/vpn_control_provider.db"
restore_file "$bot_file" "$bot_restore"
restore_file "$mediator_file" "$mediator_restore"
if [[ "$format_version" == "2" ]]; then
    restore_file "$provider_file" "$provider_restore"
fi

verify_checksum()
{
    local database_path="$1"
    local expected="$2"
    local label="$3"
    local actual
    actual="$(sha256sum "$database_path" | awk '{print $1}')"
    if [[ "$actual" != "$expected" ]]; then
        printf '%s backup checksum mismatch\n' "$label" >&2
        exit 1
    fi
}

verify_checksum "$bot_restore" "$bot_checksum" "Bot"
verify_checksum "$mediator_restore" "$mediator_checksum" "Mediator"
if [[ "$format_version" == "2" ]]; then
    verify_checksum "$provider_restore" "$provider_checksum" "Provider"
fi

check_database()
{
    local database_path="$1"
    local label="$2"
    local quick_check
    local foreign_key_check

    quick_check="$(sqlite3 "$database_path" 'PRAGMA quick_check;')"
    if [[ "$quick_check" != "ok" ]]; then
        printf '%s quick_check failed: %s\n' "$label" "$quick_check" >&2
        exit 1
    fi

    foreign_key_check="$(sqlite3 "$database_path" 'PRAGMA foreign_key_check;')"
    if [[ -n "$foreign_key_check" ]]; then
        printf '%s foreign_key_check failed:\n%s\n' "$label" "$foreign_key_check" >&2
        exit 1
    fi
}

require_table()
{
    local database_path="$1"
    local table_name="$2"
    local found
    found="$(sqlite3 "$database_path" \
        "SELECT COUNT(*) FROM sqlite_master WHERE type='table' AND name='$table_name';")"
    if [[ "$found" != "1" ]]; then
        printf 'Required table %s is missing in %s\n' "$table_name" "$database_path" >&2
        exit 1
    fi
}

check_database "$bot_restore" "Bot database"
check_database "$mediator_restore" "Mediator database"
if [[ "$format_version" == "2" ]]; then
    check_database "$provider_restore" "Provider database"
fi

for table_name in users orders subscriptions trial_claims onboarding_sessions \
    access_operation_leases product_events telegram_update_inbox commerce_policy \
    refund_plans capacity_state_transitions; do
    require_table "$bot_restore" "$table_name"
done
for table_name in mediated_subscriptions device_access_tokens connection_handoff_claims \
    mediator_migrations device_access_profiles server_health_states server_probe_observations; do
    require_table "$mediator_restore" "$table_name"
done
if [[ "$format_version" == "2" ]]; then
    for table_name in provider_migrations device_profiles provider_operations \
        provider_audit_events; do
        require_table "$provider_restore" "$table_name"
    done
fi

migration_versions()
{
    local database_path="$1"
    local table_name="$2"
    sqlite3 "$database_path" \
        "SELECT COALESCE(group_concat(version, ','), '') FROM (SELECT version FROM $table_name ORDER BY version);"
}

expected_migration_versions()
{
    local expected="$1"
    seq -s, 1 "$expected"
}

verify_migration_history()
{
    local database_path="$1"
    local table_name="$2"
    local expected="$3"
    local manifest_versions="$4"
    local manifest_fingerprint="$5"
    local label="$6"
    local actual_versions
    local expected_versions
    local actual_fingerprint

    actual_versions="$(migration_versions "$database_path" "$table_name")"
    expected_versions="$(expected_migration_versions "$expected")"
    if [[ "$actual_versions" != "$expected_versions" ]]; then
        printf '%s migration history is not the exact contiguous current set. Expected %s, got %s\n' \
            "$label" "$expected_versions" "$actual_versions" >&2
        exit 1
    fi
    if [[ -n "$manifest_versions" && "$actual_versions" != "$manifest_versions" ]]; then
        printf '%s migration history differs from the backup manifest\n' "$label" >&2
        exit 1
    fi
    actual_fingerprint="$(printf '%s' "$actual_versions" | sha256sum | awk '{print $1}')"
    if [[ -n "$manifest_fingerprint" && "$actual_fingerprint" != "$manifest_fingerprint" ]]; then
        printf '%s migration fingerprint differs from the backup manifest\n' "$label" >&2
        exit 1
    fi
}

bot_migration="$(sqlite3 "$bot_restore" \
    'SELECT COALESCE(MAX(version), 0) FROM schema_migrations;')"
mediator_migration="$(sqlite3 "$mediator_restore" \
    'SELECT COALESCE(MAX(version), 0) FROM mediator_migrations;')"
if [[ "$bot_migration" != "$EXPECTED_BOT_MIGRATION" ]]; then
    printf 'Bot backup migration %s does not equal current expected migration %s\n' \
        "$bot_migration" "$EXPECTED_BOT_MIGRATION" >&2
    exit 1
fi
if [[ "$mediator_migration" != "$EXPECTED_MEDIATOR_MIGRATION" ]]; then
    printf 'Mediator backup migration %s does not equal current expected migration %s\n' \
        "$mediator_migration" "$EXPECTED_MEDIATOR_MIGRATION" >&2
    exit 1
fi

verify_migration_history \
    "$bot_restore" schema_migrations "$EXPECTED_BOT_MIGRATION" \
    "$bot_schema_versions_manifest" "$bot_schema_fingerprint_manifest" "Bot"
verify_migration_history \
    "$mediator_restore" mediator_migrations "$EXPECTED_MEDIATOR_MIGRATION" \
    "$mediator_schema_versions_manifest" "$mediator_schema_fingerprint_manifest" "Mediator"

provider_migration=0
if [[ "$format_version" == "2" ]]; then
    provider_migration="$(sqlite3 "$provider_restore" \
        'SELECT COALESCE(MAX(version), 0) FROM provider_migrations;')"
    if (( provider_migration < EXPECTED_PROVIDER_MIGRATION )); then
        printf 'Provider backup migration %s is older than expected %s\n' \
            "$provider_migration" "$EXPECTED_PROVIDER_MIGRATION" >&2
        exit 1
    fi
fi

printf 'Restore drill passed for backup timestamp %s.\n' "$timestamp"
printf 'Bot rows: users=%s orders=%s subscriptions=%s trials=%s\n' \
    "$(sqlite3 "$bot_restore" 'SELECT COUNT(*) FROM users;')" \
    "$(sqlite3 "$bot_restore" 'SELECT COUNT(*) FROM orders;')" \
    "$(sqlite3 "$bot_restore" 'SELECT COUNT(*) FROM subscriptions;')" \
    "$(sqlite3 "$bot_restore" 'SELECT COUNT(*) FROM trial_claims;')"
printf 'Mediator rows: subscriptions=%s devices=%s profiles=%s handoffs=%s\n' \
    "$(sqlite3 "$mediator_restore" 'SELECT COUNT(*) FROM mediated_subscriptions;')" \
    "$(sqlite3 "$mediator_restore" 'SELECT COUNT(*) FROM device_access_tokens;')" \
    "$(sqlite3 "$mediator_restore" 'SELECT COUNT(*) FROM device_access_profiles;')" \
    "$(sqlite3 "$mediator_restore" 'SELECT COUNT(*) FROM connection_handoff_claims;')"
if [[ "$format_version" == "2" ]]; then
    printf 'Provider rows: profiles=%s operations=%s audit_events=%s\n' \
        "$(sqlite3 "$provider_restore" 'SELECT COUNT(*) FROM device_profiles;')" \
        "$(sqlite3 "$provider_restore" 'SELECT COUNT(*) FROM provider_operations;')" \
        "$(sqlite3 "$provider_restore" 'SELECT COUNT(*) FROM provider_audit_events;')"
fi
