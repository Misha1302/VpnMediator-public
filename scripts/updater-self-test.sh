#!/usr/bin/env bash

set -Eeuo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
UPDATER="$ROOT/update_vpn_mediator.sh"

bash -n "$UPDATER"
help_output="$("$UPDATER" --help)"
grep -q 'Usage: ./update_vpn_mediator.sh \[--check-only\]' <<<"$help_output"

if "$UPDATER" --unsupported >/dev/null 2>&1; then
    printf 'Updater accepted an unsupported argument.\n' >&2
    exit 1
fi

for required_contract in \
    'deploy/backup.sh' \
    'unit_backup_dir' \
    'previous_mediator' \
    'previous_bot' \
    'source_sha256' \
    'chmod -R a+rX' \
    'switched=true' \
    '--no-build-isolation' \
    'environment contains removed settings'; do
    grep -q -- "$required_contract" "$UPDATER" || {
        printf 'Updater safety contract is missing: %s\n' "$required_contract" >&2
        exit 1
    }
done

if grep -q -E 'vpn-probe-agent|PROBE_SERVICE|prepare_probe' "$UPDATER"; then
    printf 'Updater still manages the removed Probe Agent.\n' >&2
    exit 1
fi

switched_line="$(grep -n '^switched=true$' "$UPDATER" | tail -1 | cut -d: -f1)"
first_switch_line="$(grep -n "switch_link \"\$mediator_release\"" "$UPDATER" | cut -d: -f1)"
permissions_line="$(grep -n 'chmod -R a+rX' "$UPDATER" | cut -d: -f1)"
release_move_line="$(grep -n "mv \"\$mediator_stage\"" "$UPDATER" | cut -d: -f1)"
if ((switched_line >= first_switch_line)); then
    printf 'Rollback is not armed before the first release-link switch.\n' >&2
    exit 1
fi
if ((permissions_line >= release_move_line)); then
    printf 'Release permissions are not fixed before installation.\n' >&2
    exit 1
fi

printf 'Updater self-test passed.\n'
