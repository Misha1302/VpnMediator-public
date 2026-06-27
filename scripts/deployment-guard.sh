#!/usr/bin/env bash

set -Eeuo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

fail()
{
    printf 'Deployment guard failed: %s\n' "$1" >&2
    exit 1
}

[[ -f global.json ]] || fail 'global.json is missing'
grep -q '"version": "10.0.301"' global.json || fail '.NET SDK is not pinned'
[[ -f deploy/vpnmediator.service ]] || fail 'Mediator unit is missing'
[[ -f deploy/vpn-access-bot.service ]] || fail 'Bot unit is missing'
[[ ! -e deploy/vpn-mediator.service ]] || fail 'legacy duplicate Mediator unit exists'
grep -q '^Alias=vpn-mediator.service$' deploy/vpnmediator.service \
    || fail 'Mediator compatibility alias is missing'
grep -q '/opt/vpn-mediator/current/VpnMediator.dll$' deploy/vpnmediator.service \
    || fail 'Mediator unit does not use an atomic release link'
grep -q '/opt/vpn-access-bot/current/.venv/bin/python' deploy/vpn-access-bot.service \
    || fail 'Bot unit does not use an atomic release link'

if find . -path './.git' -prune -o -type f \
    \( -iname '*probe-agent*' -o -iname '*server-health*' \) -print | grep -q .; then
    fail 'removed Probe Agent deployment artifacts are present'
fi
if grep -R -n --binary-files=without-match \
    -E 'vpn-probe-agent|PROBE_SERVICE|prepare_probe|ManagedDeviceProvisioning' \
    deploy .github/workflows; then
    fail 'deployment still references a removed service or provider'
fi

grep -q 'deploy/backup.sh' update_vpn_mediator.sh \
    || fail 'updater does not create a coordinated database backup'
grep -q 'rollback' update_vpn_mediator.sh \
    || fail 'updater has no release-link rollback path'
grep -q 'unit_backup_dir' update_vpn_mediator.sh \
    || fail 'updater does not preserve previous systemd units for rollback'
grep -q -- '--no-build-isolation' update_vpn_mediator.sh \
    || fail 'updater does not build the Bot wheel from pinned build requirements'
grep -q 'source_sha256' update_vpn_mediator.sh \
    || fail 'updater does not record source provenance in deployed releases'
grep -q 'environment contains removed settings' update_vpn_mediator.sh \
    || fail 'updater does not reject obsolete production configuration'
grep -q 'proxy_no_cache 1;' deploy/nginx.conf.example \
    || fail 'subscription responses may be cached by nginx'
grep -q '^  release-gate:$' .github/workflows/ci.yml \
    || fail 'release gate is absent from CI'
grep -q './scripts/validate-release.sh' .github/workflows/ci.yml \
    || fail 'CI does not run the canonical validator'

printf 'Deployment guard passed.\n'
