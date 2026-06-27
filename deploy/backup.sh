#!/usr/bin/env bash

set -Eeuo pipefail
umask 077

BACKUP_DIR="${BACKUP_DIR:-/var/backups/vpn}"
BOT_DATABASE_PATH="${BOT_DATABASE_PATH:-/var/lib/vpn-access-bot/vpn_bot.db}"
MEDIATOR_DATABASE_PATH="${MEDIATOR_DATABASE_PATH:-/var/lib/vpn-mediator/vpn-mediator.db}"
BACKUP_RETENTION_DAYS="${BACKUP_RETENTION_DAYS:-14}"
BACKUP_AGE_RECIPIENT="${BACKUP_AGE_RECIPIENT:-}"
BACKUP_QUIESCE_UNITS="${BACKUP_QUIESCE_UNITS:-vpn-access-bot.service vpnmediator.service}"
BACKUP_ALLOW_UNQUIESCED="${BACKUP_ALLOW_UNQUIESCED:-false}"

for command_name in sqlite3 sha256sum; do
    command -v "$command_name" >/dev/null 2>&1 || {
        printf '%s is required\n' "$command_name" >&2
        exit 1
    }
done
if [[ -n "$BACKUP_AGE_RECIPIENT" ]]; then
    command -v age >/dev/null 2>&1 || {
        printf 'age is required when BACKUP_AGE_RECIPIENT is configured\n' >&2
        exit 1
    }
fi

declare -a configured_units=()
declare -a quiesced_units=()
services_resumed=false
read -r -a configured_units <<<"$BACKUP_QUIESCE_UNITS"

validate_unit_name()
{
    local unit_name="$1"
    [[ "$unit_name" =~ ^[A-Za-z0-9_.@-]+$ ]] || {
        printf 'Invalid systemd unit name in BACKUP_QUIESCE_UNITS: %s\n' "$unit_name" >&2
        return 1
    }
}

unit_was_quiesced()
{
    local wanted="$1"
    local current
    for current in "${quiesced_units[@]}"; do
        [[ "$current" == "$wanted" ]] && return 0
    done
    return 1
}

resume_quiesced_services()
{
    [[ "$services_resumed" == "true" ]] && return 0
    local index
    local failed=0
    for ((index=${#quiesced_units[@]}-1; index>=0; index--)); do
        if ! systemctl start "${quiesced_units[index]}"; then
            printf 'CRITICAL: failed to restart %s after backup\n' \
                "${quiesced_units[index]}" >&2
            failed=1
        fi
    done
    services_resumed=true
    return "$failed"
}

quiesce_services()
{
    if ((${#configured_units[@]} == 0)); then
        if [[ "$BACKUP_ALLOW_UNQUIESCED" == "true" ]]; then
            return 0
        fi
        printf 'BACKUP_QUIESCE_UNITS is empty; refusing an inconsistent two-database backup\n' >&2
        return 1
    fi

    command -v systemctl >/dev/null 2>&1 || {
        printf 'systemctl is required for a coordinated two-database backup\n' >&2
        return 1
    }

    local unit_name
    for unit_name in "${configured_units[@]}"; do
        validate_unit_name "$unit_name"
        if systemctl is-active --quiet "$unit_name"; then
            systemctl stop "$unit_name"
            if systemctl is-active --quiet "$unit_name"; then
                printf 'Service did not stop for coordinated backup: %s\n' "$unit_name" >&2
                return 1
            fi
            quiesced_units+=("$unit_name")
        fi
    done
}

install -d -m 0700 "$BACKUP_DIR"
timestamp="$(date -u +%Y%m%dT%H%M%SZ)"
staging_directory="$(mktemp -d "$BACKUP_DIR/.pair-${timestamp}.XXXXXX")"
cleanup()
{
    local status="$?"
    set +e
    if ! resume_quiesced_services; then
        status=1
    fi
    rm -rf -- "$staging_directory"
    trap - EXIT
    exit "$status"
}
trap cleanup EXIT

backup_database()
{
    local source_path="$1"
    local output_path="$2"

    if [[ ! -f "$source_path" ]]; then
        printf 'Database not found: %s\n' "$source_path" >&2
        return 1
    fi

    sqlite3 "$source_path" ".timeout 10000" ".backup '$output_path'"

    local check_result
    check_result="$(sqlite3 "$output_path" 'PRAGMA quick_check;')"
    if [[ "$check_result" != "ok" ]]; then
        printf 'Backup integrity check failed for %s: %s\n' "$source_path" "$check_result" >&2
        return 1
    fi
    chmod 0600 "$output_path"
}

table_exists()
{
    local database_path="$1"
    local table_name="$2"

    sqlite3 "$database_path" \
        "SELECT COUNT(*) FROM sqlite_master WHERE type = 'table' AND name = '$table_name';"
}

schema_max_version()
{
    local database_path="$1"
    local table_name="$2"

    if [[ "$(table_exists "$database_path" "$table_name")" != "1" ]]; then
        printf '0\n'
        return 0
    fi

    sqlite3 "$database_path" \
        "SELECT COALESCE(MAX(version), 0) FROM $table_name;"
}

migration_versions()
{
    local database_path="$1"
    local table_name="$2"

    if [[ "$(table_exists "$database_path" "$table_name")" != "1" ]]; then
        printf '\n'
        return 0
    fi

    sqlite3 "$database_path" \
        "SELECT COALESCE(group_concat(version, ','), '') FROM (SELECT version FROM $table_name ORDER BY version);"
}

migration_fingerprint()
{
    local versions="$1"
    printf '%s' "$versions" | sha256sum | awk '{print $1}'
}

bot_name="vpn_bot.${timestamp}.db"
mediator_name="vpn_mediator.${timestamp}.db"
manifest_name="vpn_pair.${timestamp}.manifest"

quiesce_services
backup_database "$BOT_DATABASE_PATH" "$staging_directory/$bot_name"
backup_database "$MEDIATOR_DATABASE_PATH" "$staging_directory/$mediator_name"
resume_quiesced_services

bot_schema="$(schema_max_version "$staging_directory/$bot_name" schema_migrations)"
mediator_schema="$(schema_max_version "$staging_directory/$mediator_name" mediator_migrations)"
bot_schema_versions="$(migration_versions "$staging_directory/$bot_name" schema_migrations)"
mediator_schema_versions="$(migration_versions "$staging_directory/$mediator_name" mediator_migrations)"
bot_schema_fingerprint="$(migration_fingerprint "$bot_schema_versions")"
mediator_schema_fingerprint="$(migration_fingerprint "$mediator_schema_versions")"
bot_checksum="$(sha256sum "$staging_directory/$bot_name" | awk '{print $1}')"
mediator_checksum="$(sha256sum "$staging_directory/$mediator_name" | awk '{print $1}')"

cat > "$staging_directory/$manifest_name" <<EOF_MANIFEST
format_version=3
timestamp_utc=$timestamp
snapshot_mode=systemd_quiesced
quiesced_units=${quiesced_units[*]}
bot_file=$bot_name
bot_sha256=$bot_checksum
bot_schema_version=$bot_schema
bot_schema_versions=$bot_schema_versions
bot_schema_fingerprint=$bot_schema_fingerprint
mediator_file=$mediator_name
mediator_sha256=$mediator_checksum
mediator_schema_version=$mediator_schema
mediator_schema_versions=$mediator_schema_versions
mediator_schema_fingerprint=$mediator_schema_fingerprint
encrypted=$([[ -n "$BACKUP_AGE_RECIPIENT" ]] && printf true || printf false)
EOF_MANIFEST
chmod 0600 "$staging_directory/$manifest_name"

if [[ -n "$BACKUP_AGE_RECIPIENT" ]]; then
    for database_name in "$bot_name" "$mediator_name"; do
        age -r "$BACKUP_AGE_RECIPIENT" \
            -o "$staging_directory/$database_name.age" \
            "$staging_directory/$database_name"
        rm -f -- "$staging_directory/$database_name"
    done
    sed -i \
        -e "s/^bot_file=.*/bot_file=$bot_name.age/" \
        -e "s/^mediator_file=.*/mediator_file=$mediator_name.age/" \
        "$staging_directory/$manifest_name"
fi

# Publish the manifest last. Restore tooling only accepts complete bundles.
for file_path in "$staging_directory"/*; do
    [[ "$(basename "$file_path")" == "$manifest_name" ]] && continue
    mv -- "$file_path" "$BACKUP_DIR/"
done
mv -- "$staging_directory/$manifest_name" "$BACKUP_DIR/"

printf 'Created verified backup pair: %s\n' "$BACKUP_DIR/$manifest_name"

find "$BACKUP_DIR" -maxdepth 1 -type f \
    \( -name 'vpn_pair.*.manifest' -o -name 'vpn_bundle.*.manifest' \) \
    -mtime "+$BACKUP_RETENTION_DAYS" -print0 |
while IFS= read -r -d '' old_manifest; do
    old_bot="$(awk -F= '$1 == "bot_file" { print $2 }' "$old_manifest")"
    old_mediator="$(awk -F= '$1 == "mediator_file" { print $2 }' "$old_manifest")"
    old_provider="$(awk -F= '$1 == "provider_file" { print $2 }' "$old_manifest")"
    rm -f -- "$BACKUP_DIR/$old_bot" "$BACKUP_DIR/$old_mediator"
    if [[ -n "$old_provider" ]]; then
        rm -f -- "$BACKUP_DIR/$old_provider"
    fi
    rm -f -- "$old_manifest"
done
