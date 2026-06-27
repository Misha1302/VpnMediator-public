#!/usr/bin/env bash
set -Eeuo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

common_excludes=(
  --exclude-dir=.git
  --exclude-dir=.venv
  --exclude-dir=bin
  --exclude-dir=obj
  --exclude-dir=__pycache__
  --exclude-dir=.pytest_cache
  --exclude-dir=.ruff_cache
  --exclude-dir=tests
  --exclude='*.db'
  --exclude='*.wal'
  --exclude='*.shm'
  --exclude='*.example'
  --exclude='*.md'
  --exclude='uv.lock'
)

scan() {
  local pattern="$1"
  grep -RInE "${common_excludes[@]}" -- "$pattern" .
}

failed=0
if scan '[0-9]{8,10}:[A-Za-z0-9_-]{30,}'; then failed=1; fi
if scan '-----BEGIN (RSA |EC |OPENSSH )?PRIVATE KEY-----'; then failed=1; fi
if scan 'https?://[^[:space:]]+/sub/[^[:space:]]+[?&]token=[A-Za-z0-9_-]{16,}'; then failed=1; fi
if find . -type f \( -name '.env' -o -name '*.pem' -o -name '*.key' -o -name '*.p12' \) \
    -not -path './.git/*' \
    -not -path './.venv/*' \
    -not -path './VpnAccessBot/.venv/*' \
    -not -path './bin/*' \
    -not -path './obj/*' \
    -print -quit | grep -q .; then
  printf 'Runtime secret/certificate file detected.\n' >&2
  failed=1
fi

if (( failed != 0 )); then
  printf 'Potential secret material detected.\n' >&2
  exit 1
fi
printf 'Secret scan passed.\n'
