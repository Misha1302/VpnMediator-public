#!/usr/bin/env bash

set -Eeuo pipefail
umask 077

export DOTNET_CLI_DO_NOT_USE_MSBUILD_SERVER=1
export MSBUILDDISABLENODEREUSE=1
export PIP_DISABLE_PIP_VERSION_CHECK=1

REPOSITORY_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPOSITORY_ROOT"

PYTHON_BIN="${PYTHON_BIN:-python3}"
NUGET_CONFIG_FILE="${NUGET_CONFIG_FILE:-}"
SKIP_ONLINE_ADVISORY_CHECKS="${SKIP_ONLINE_ADVISORY_CHECKS:-0}"
VALIDATION_VENV="${VALIDATION_VENV:-}"

for command_name in "$PYTHON_BIN" dotnet shellcheck; do
    command -v "$command_name" >/dev/null 2>&1 || {
        printf '%s is required\n' "$command_name" >&2
        exit 1
    }
done

"$PYTHON_BIN" - <<'PY'
import sys

version = sys.version_info[:2]
if not (version >= (3, 11) and version < (3, 14)):
    print(
        f"Python 3.11, 3.12, or 3.13 is required; current interpreter is {sys.version.split()[0]}",
        file=sys.stderr,
    )
    raise SystemExit(1)
PY

run_pytest_suite_bounded()
{
    local test_directory="$1"
    local timeout_seconds="${PYTEST_BATCH_TIMEOUT_SECONDS:-300}"
    local batch_size="${PYTEST_BATCH_SIZE:-5}"

    "$PYTHON_BIN" "$REPOSITORY_ROOT/scripts/run-pytest-suite-bounded.py" \
        --python "$venv/bin/python" \
        --test-directory "$test_directory" \
        --timeout-seconds "$timeout_seconds" \
        --batch-size "$batch_size"
}

validation_root=""
temporary_wheel_root=""
if [[ -n "$VALIDATION_VENV" ]]; then
    venv="$VALIDATION_VENV"
    [[ -x "$venv/bin/python" ]] || {
        printf 'VALIDATION_VENV does not contain an executable Python interpreter: %s\n' "$venv" >&2
        exit 1
    }
else
    validation_root="$(mktemp -d /tmp/vpnmediator-release-validation.XXXXXX)"
    venv="$validation_root/venv"
    "$PYTHON_BIN" -m venv "$venv"
fi
cleanup()
{
    if [[ -n "$validation_root" ]]; then
        rm -rf -- "$validation_root"
    fi
    if [[ -n "$temporary_wheel_root" ]]; then
        rm -rf -- "$temporary_wheel_root"
    fi
}
trap cleanup EXIT

"$PYTHON_BIN" scripts/verify-python-lock.py
"$venv/bin/python" -m pip install --require-hashes -r VpnAccessBot/requirements.lock
"$venv/bin/python" -m pip install --require-hashes -r VpnAccessBot/build-requirements.lock
if [[ -n "$validation_root" ]]; then
    wheel_dir="$validation_root/wheel"
else
    temporary_wheel_root="$(mktemp -d /tmp/vpnmediator-wheel.XXXXXX)"
    wheel_dir="$temporary_wheel_root/wheel"
fi
mkdir -p "$wheel_dir"
"$venv/bin/python" -m pip wheel \
    --no-deps \
    --no-build-isolation \
    --wheel-dir "$wheel_dir" \
    ./VpnAccessBot
rm -rf VpnAccessBot/build VpnAccessBot/vpn_access_bot.egg-info
mapfile -t bot_wheels < <(find "$wheel_dir" -maxdepth 1 -type f -name 'vpn_access_bot-*.whl' -print)
[[ "${#bot_wheels[@]}" -eq 1 ]] || {
    printf 'Expected exactly one VpnAccessBot wheel, found %s\n' "${#bot_wheels[@]}" >&2
    exit 1
}
"$venv/bin/python" -m pip install --force-reinstall --no-deps "${bot_wheels[0]}"
(
    cd /tmp
    "$venv/bin/python" - <<'PY'
from pathlib import Path

import vpn_access_bot

module_path = Path(vpn_access_bot.__file__).resolve()
if "site-packages" not in module_path.parts:
    raise SystemExit(f"Wheel import did not resolve from site-packages: {module_path}")
print(f"Verified installed wheel import: {module_path}")
PY
)

./scripts/architecture-guard.sh
"$venv/bin/python" scripts/check-public-branding.py
./scripts/secret-scan.sh
"$venv/bin/python" -m compileall scripts
"$venv/bin/ruff" check --config VpnAccessBot/pyproject.toml scripts
"$venv/bin/ruff" format --check --config VpnAccessBot/pyproject.toml scripts

(
    cd VpnAccessBot
    "$venv/bin/python" -m compileall vpn_access_bot
    "$venv/bin/ruff" check .
    "$venv/bin/ruff" format --check .
    run_pytest_suite_bounded tests
    if [[ "$SKIP_ONLINE_ADVISORY_CHECKS" == "1" ]]; then
        printf 'Skipping online Python advisory lookup by explicit request.\n'
    else
        "$venv/bin/pip-audit"
    fi
)

./scripts/deployment-guard.sh
./scripts/backup-restore-self-test.sh
PYTHON_BIN="$venv/bin/python" ./scripts/release-evidence-self-test.sh

"$venv/bin/python" scripts/generate-sbom.py

restore_arguments=()
if [[ -n "$NUGET_CONFIG_FILE" ]]; then
    restore_arguments+=(--configfile "$NUGET_CONFIG_FILE")
fi
if [[ "$SKIP_ONLINE_ADVISORY_CHECKS" == "1" ]]; then
    restore_arguments+=(-p:NuGetAudit=false)
fi

test_project="VpnMediator.Tests/VpnMediator.Tests.csproj"
timeout 15s dotnet build-server shutdown >/dev/null 2>&1 || true
dotnet restore "$test_project" --disable-parallel "${restore_arguments[@]}"
dotnet build "$test_project" \
    --configuration Release \
    --no-restore \
    -warnaserror \
    -m:1 \
    --disable-build-servers \
    -p:UseSharedCompilation=false \
    -p:BuildInParallel=false
dotnet test "$test_project" \
    --configuration Release \
    --no-build \
    --no-restore \
    -m:1 \
    --disable-build-servers \
    -p:UseSharedCompilation=false
if [[ "$SKIP_ONLINE_ADVISORY_CHECKS" == "1" ]]; then
    printf 'Skipping online NuGet advisory lookup for %s by explicit request.\n' "$test_project"
else
    dotnet list "$test_project" package \
        --vulnerable \
        --include-transitive \
        --no-restore
fi

while IFS= read -r -d '' script_path; do
    bash -n "$script_path"
done < <(find . -type f -name '*.sh' -not -path './.git/*' -print0)

find . -type f -name '*.sh' -not -path './.git/*' -print0 | xargs -0 -r shellcheck

printf 'Release validation passed.\n'
