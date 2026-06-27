#!/usr/bin/env bash

set -Eeuo pipefail
umask 077

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CHECK_ONLY=false
MEDIATOR_ROOT="${MEDIATOR_DEPLOY_DIR:-/opt/vpn-mediator}"
BOT_ROOT="${BOT_DEPLOY_DIR:-/opt/vpn-access-bot}"
MEDIATOR_UNIT="${MEDIATOR_SERVICE:-vpnmediator.service}"
BOT_UNIT="${BOT_SERVICE:-vpn-access-bot.service}"
MEDIATOR_READY_URL="${MEDIATOR_READY_URL:-http://127.0.0.1:5062/health/ready}"
BOT_LIVE_URL="${BOT_LIVE_URL:-http://127.0.0.1:8081/health/live}"

usage()
{
    cat <<'EOF'
Usage: ./update_vpn_mediator.sh [--check-only]

Validates the current source tree and deploys two atomic releases: VpnMediator
and VpnAccessBot. Production secrets remain in /etc and both SQLite databases
remain in /var/lib. A coordinated database backup is created before switching.
EOF
}

while (($#)); do
    case "$1" in
        --check-only) CHECK_ONLY=true ;;
        --help|-h) usage; exit 0 ;;
        *) printf 'Unknown argument: %s\n' "$1" >&2; usage >&2; exit 2 ;;
    esac
    shift
done

required=(
    awk basename bash chmod cp curl date dotnet find grep install ln mktemp mv python3 readlink rm
    sha256sum sort sqlite3 systemctl xargs
)
for command_name in "${required[@]}"; do
    command -v "$command_name" >/dev/null 2>&1 || {
        printf 'Required command is missing: %s\n' "$command_name" >&2
        exit 1
    }
done

cd "$ROOT"
./scripts/validate-release.sh
if "$CHECK_ONLY"; then
    printf 'Source and release checks passed; no files or services were changed.\n'
    exit 0
fi

if ((EUID != 0)); then
    printf 'Deployment must run as root. Use --check-only for local validation.\n' >&2
    exit 1
fi
for path in /etc/vpn-mediator/mediator.env /etc/vpn-access-bot/bot.env; do
    [[ -s "$path" ]] || { printf 'Required environment file is missing: %s\n' "$path" >&2; exit 1; }
done

if grep -Eq \
    '^VpnMediator__(ServerHealth|DeviceAccessMode|ManagedDeviceProvisioning|UnifiedSubscriptionFeedEnabled|FileLogging)' \
    /etc/vpn-mediator/mediator.env; then
    printf '%s\n' \
        'Mediator environment contains removed settings. Migrate it from deploy/mediator.env.example before deployment.' \
        >&2
    exit 1
fi
if grep -Eq \
    '^(COMMERCE_MINIMUM_HEALTHY_SERVERS|TELEGRAM_PROVIDER_TOKEN|SUPPORT_TELEGRAM_USERNAME)=' \
    /etc/vpn-access-bot/bot.env; then
    printf '%s\n' \
        'Bot environment contains removed settings. Migrate it from deploy/bot.env.example before deployment.' \
        >&2
    exit 1
fi

for legacy_unit in \
    /etc/systemd/system/vpn-mediator.service \
    /usr/lib/systemd/system/vpn-mediator.service \
    /lib/systemd/system/vpn-mediator.service; do
    if [[ -L "$legacy_unit" ]]; then
        resolved_unit="$(readlink -f "$legacy_unit" 2>/dev/null || true)"
        if [[ "$(basename "$resolved_unit")" != "vpnmediator.service" ]]; then
            printf 'Unexpected Mediator alias target: %s -> %s\n' \
                "$legacy_unit" "${resolved_unit:-broken}" >&2
            exit 1
        fi
    elif [[ -e "$legacy_unit" ]]; then
        printf 'Competing physical Mediator unit must be removed before deployment: %s\n' \
            "$legacy_unit" >&2
        exit 1
    fi
done

source_sha="$({
    find . -type f \
        -not -path './.git/*' \
        -not -path './.venv/*' \
        -not -path '*/__pycache__/*' \
        -not -path './release/current-release-evidence.md' \
        -not -path './release/source-tree.sha256' \
        -print0 \
        | sort -z \
        | xargs -0 sha256sum
} | sha256sum | awk '{print $1}')"
release_id="$(date -u +%Y%m%dT%H%M%SZ)-${source_sha:0:12}"
build_root="$(mktemp -d /tmp/vpnmediator-deploy.XXXXXX)"
mediator_stage="$build_root/mediator"
bot_stage="$build_root/bot"
builder_venv="$build_root/builder-venv"
wheel_dir="$build_root/wheel"
unit_backup_dir="$build_root/systemd"
mkdir -p "$mediator_stage" "$bot_stage" "$wheel_dir" "$unit_backup_dir"

cleanup()
{
    rm -rf -- "$build_root"
}
trap cleanup EXIT

dotnet publish VpnMediator.csproj \
    --configuration Release \
    --output "$mediator_stage" \
    --no-restore
printf 'source_sha256=%s\nrelease_id=%s\n' "$source_sha" "$release_id" \
    > "$mediator_stage/release-metadata.txt"

python3 -m venv "$builder_venv"
"$builder_venv/bin/python" -m pip install \
    --require-hashes -r VpnAccessBot/build-requirements.lock
"$builder_venv/bin/python" -m pip wheel \
    --no-deps \
    --no-build-isolation \
    --wheel-dir "$wheel_dir" \
    ./VpnAccessBot
mapfile -t bot_wheels < <(find "$wheel_dir" -maxdepth 1 -type f -name 'vpn_access_bot-*.whl')
[[ "${#bot_wheels[@]}" -eq 1 ]] || {
    printf 'Expected exactly one VpnAccessBot wheel, found %s\n' "${#bot_wheels[@]}" >&2
    exit 1
}

python3 -m venv "$bot_stage/.venv"
"$bot_stage/.venv/bin/python" -m pip install \
    --require-hashes -r VpnAccessBot/requirements.lock
"$bot_stage/.venv/bin/python" -m pip install --no-deps "${bot_wheels[0]}"
install -m 0644 VpnAccessBot/requirements.lock "$bot_stage/requirements.lock"
install -m 0644 "${bot_wheels[0]}" "$bot_stage/"
printf 'source_sha256=%s\nrelease_id=%s\nwheel_sha256=%s\n' \
    "$source_sha" \
    "$release_id" \
    "$(sha256sum "${bot_wheels[0]}" | awk '{print $1}')" \
    > "$bot_stage/release-metadata.txt"
(
    cd /tmp
    "$bot_stage/.venv/bin/python" -c \
        'from pathlib import Path; import vpn_access_bot; assert "site-packages" in Path(vpn_access_bot.__file__).parts'
)
chmod -R a+rX "$mediator_stage" "$bot_stage"

mediator_release="$MEDIATOR_ROOT/releases/$release_id"
bot_release="$BOT_ROOT/releases/$release_id"
[[ ! -e "$mediator_release" ]] || { printf 'Release already exists: %s\n' "$mediator_release" >&2; exit 1; }
[[ ! -e "$bot_release" ]] || { printf 'Release already exists: %s\n' "$bot_release" >&2; exit 1; }

mediator_was_active=false
bot_was_active=false
mediator_was_enabled=false
bot_was_enabled=false
systemctl is-active --quiet "$MEDIATOR_UNIT" && mediator_was_active=true
systemctl is-active --quiet "$BOT_UNIT" && bot_was_active=true
systemctl is-enabled --quiet "$MEDIATOR_UNIT" && mediator_was_enabled=true
systemctl is-enabled --quiet "$BOT_UNIT" && bot_was_enabled=true

mediator_unit_existed=false
bot_unit_existed=false
if [[ -e /etc/systemd/system/vpnmediator.service ]]; then
    cp -a /etc/systemd/system/vpnmediator.service "$unit_backup_dir/vpnmediator.service"
    mediator_unit_existed=true
fi
if [[ -e /etc/systemd/system/vpn-access-bot.service ]]; then
    cp -a /etc/systemd/system/vpn-access-bot.service "$unit_backup_dir/vpn-access-bot.service"
    bot_unit_existed=true
fi

BACKUP_QUIESCE_UNITS="$BOT_UNIT $MEDIATOR_UNIT" deploy/backup.sh

install -d -m 0755 "$MEDIATOR_ROOT/releases" "$BOT_ROOT/releases"
mv "$mediator_stage" "$mediator_release"
mv "$bot_stage" "$bot_release"

previous_mediator="$(readlink -f "$MEDIATOR_ROOT/current" 2>/dev/null || true)"
previous_bot="$(readlink -f "$BOT_ROOT/current" 2>/dev/null || true)"
switched=false

switch_link()
{
    local target="$1"
    local link="$2"
    local temporary="${link}.new.$$"
    ln -s "$target" "$temporary"
    mv -Tf "$temporary" "$link"
}

rollback()
{
    local status="$?"
    if [[ "$switched" == true && "$status" != 0 ]]; then
        set +e
        printf 'Deployment failed; rolling release links back.\n' >&2
        systemctl stop "$BOT_UNIT" "$MEDIATOR_UNIT" >/dev/null 2>&1 || true
        if [[ -n "$previous_mediator" ]]; then
            switch_link "$previous_mediator" "$MEDIATOR_ROOT/current"
        else
            rm -f "$MEDIATOR_ROOT/current"
        fi
        if [[ -n "$previous_bot" ]]; then
            switch_link "$previous_bot" "$BOT_ROOT/current"
        else
            rm -f "$BOT_ROOT/current"
        fi
        if [[ "$mediator_unit_existed" == true ]]; then
            cp -a "$unit_backup_dir/vpnmediator.service" \
                /etc/systemd/system/vpnmediator.service
        else
            rm -f /etc/systemd/system/vpnmediator.service
        fi
        if [[ "$bot_unit_existed" == true ]]; then
            cp -a "$unit_backup_dir/vpn-access-bot.service" \
                /etc/systemd/system/vpn-access-bot.service
        else
            rm -f /etc/systemd/system/vpn-access-bot.service
        fi
        systemctl daemon-reload
        if [[ "$mediator_was_enabled" == true ]]; then
            systemctl enable "$MEDIATOR_UNIT" >/dev/null 2>&1 || true
        else
            systemctl disable "$MEDIATOR_UNIT" >/dev/null 2>&1 || true
        fi
        if [[ "$bot_was_enabled" == true ]]; then
            systemctl enable "$BOT_UNIT" >/dev/null 2>&1 || true
        else
            systemctl disable "$BOT_UNIT" >/dev/null 2>&1 || true
        fi
        if [[ "$mediator_was_active" == true ]]; then
            systemctl start "$MEDIATOR_UNIT" >/dev/null 2>&1 || true
        fi
        if [[ "$bot_was_active" == true ]]; then
            systemctl start "$BOT_UNIT" >/dev/null 2>&1 || true
        fi
    fi
    exit "$status"
}
trap rollback ERR

systemctl stop "$BOT_UNIT" "$MEDIATOR_UNIT"
switched=true
switch_link "$mediator_release" "$MEDIATOR_ROOT/current"
switch_link "$bot_release" "$BOT_ROOT/current"

install -m 0644 deploy/vpnmediator.service /etc/systemd/system/vpnmediator.service
install -m 0644 deploy/vpn-access-bot.service /etc/systemd/system/vpn-access-bot.service
systemctl daemon-reload
systemctl enable "$MEDIATOR_UNIT" "$BOT_UNIT" >/dev/null
systemctl start "$MEDIATOR_UNIT"

for _ in {1..30}; do
    curl --fail --silent --show-error "$MEDIATOR_READY_URL" >/dev/null && break
    sleep 2
done
curl --fail --silent --show-error "$MEDIATOR_READY_URL" >/dev/null

systemctl start "$BOT_UNIT"
for _ in {1..15}; do
    curl --fail --silent --show-error "$BOT_LIVE_URL" >/dev/null && break
    sleep 2
done
curl --fail --silent --show-error "$BOT_LIVE_URL" >/dev/null

switched=false
trap - ERR
printf 'Deployment completed: %s\n' "$release_id"
