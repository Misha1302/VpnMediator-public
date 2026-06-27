#!/usr/bin/env bash
set -Eeuo pipefail

if (( $# < 1 || $# > 2 )); then
    printf 'Usage: %s primary-host [fallback-host]\n' "$0" >&2
    exit 2
fi

for host in "$@"; do
    [[ -n "$host" ]] || continue
    getent ahosts "$host" >/dev/null
    expiry="$(timeout 10 openssl s_client -servername "$host" -connect "$host:443" </dev/null 2>/dev/null |
        openssl x509 -noout -enddate | cut -d= -f2-)"
    [[ -n "$expiry" ]] || { printf 'Unable to read TLS certificate for %s\n' "$host" >&2; exit 1; }
    expiry_epoch="$(date -d "$expiry" +%s)"
    now_epoch="$(date +%s)"
    days_left="$(( (expiry_epoch - now_epoch) / 86400 ))"
    printf '%s dns=ok tls_days_left=%s\n' "$host" "$days_left"
    (( days_left >= 14 )) || exit 1
done
