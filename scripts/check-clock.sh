#!/usr/bin/env bash
set -Eeuo pipefail

command -v timedatectl >/dev/null || { printf 'timedatectl is required\n' >&2; exit 1; }
synchronized="$(timedatectl show -p NTPSynchronized --value)"
if [[ "$synchronized" != "yes" ]]; then
    printf 'NTP is not synchronized\n' >&2
    exit 1
fi
printf 'NTP synchronized\n'
