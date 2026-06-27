#!/usr/bin/env bash

set -Eeuo pipefail

if (( $# != 1 )); then
    printf 'Usage: %s https://public-vpn-host.example\n' "$0" >&2
    exit 64
fi

base_url="${1%/}"
case "$base_url" in
    https://*) ;;
    *)
        printf 'The public base URL must use HTTPS.\n' >&2
        exit 64
        ;;
esac

status_code="$(curl --silent --show-error --output /dev/null --write-out '%{http_code}' \
    --max-time 10 "$base_url/admin/health")"
case "$status_code" in
    000|401|403|404) ;;
    *)
        printf 'Public admin route is unexpectedly reachable (HTTP %s).\n' "$status_code" >&2
        exit 1
        ;;
esac
printf 'External admin denial check passed (HTTP %s).\n' "$status_code"
