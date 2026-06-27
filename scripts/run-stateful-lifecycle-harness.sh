#!/usr/bin/env bash

set -Eeuo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT/VpnAccessBot"

export PYTEST_DISABLE_PLUGIN_AUTOLOAD=1
export PYTHONPATH="${PYTHONPATH:-.}"
python3 -m pytest -p pytest_asyncio.plugin -q \
    tests/test_stateful_advertising_lifecycle.py
