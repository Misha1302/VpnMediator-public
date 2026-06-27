#!/usr/bin/env bash
set -Eeuo pipefail

if [[ "${CONFIRM_ALERT_FIRE_DRILL:-}" != "YES" ]]; then
    printf 'Set CONFIRM_ALERT_FIRE_DRILL=YES to emit a synthetic critical alert.\n' >&2
    exit 2
fi
logger -p user.crit -t vpnmediator-fire-drill \
    'Synthetic VPNMEDIATOR_ALERT_FIRE_DRILL. Verify delivery and close the incident.'
printf 'Synthetic alert written to journald. Delivery must be verified externally.\n'
