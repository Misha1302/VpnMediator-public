#!/usr/bin/env bash

set -Eeuo pipefail
umask 077

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PYTHON_BIN="${PYTHON_BIN:-python3}"
TMP_DIR="$(mktemp -d /tmp/vpn-release-evidence-test.XXXXXX)"
cleanup()
{
    rm -rf -- "$TMP_DIR"
}
trap cleanup EXIT

artifact="$TMP_DIR/example.tar.gz"
checksum="$artifact.sha256"
manifest="$artifact.manifest.json"
evidence="$artifact.release-evidence.md"
printf 'immutable artifact\n' > "$artifact"
artifact_sha="$(sha256sum "$artifact" | awk '{print $1}')"
source_sha="$(printf 'source-tree' | sha256sum | awk '{print $1}')"
printf '%s  %s\n' "$artifact_sha" "$(basename "$artifact")" > "$checksum"
cat > "$evidence" <<EOF
# Current release evidence

- Source tree SHA-256: \`$source_sha\`
- Artifact: \`$(basename "$artifact")\`
- Artifact size: \`$(stat -c %s "$artifact")\` bytes
- Artifact SHA-256: \`$artifact_sha\`
EOF
evidence_sha="$(sha256sum "$evidence" | awk '{print $1}')"
"$PYTHON_BIN" - "$manifest" "$artifact" "$artifact_sha" "$source_sha" "$evidence" "$evidence_sha" <<'PY'
import json
import sys
from pathlib import Path

manifest = Path(sys.argv[1])
artifact = Path(sys.argv[2])
payload = {
    "artifact": artifact.name,
    "artifactSizeBytes": artifact.stat().st_size,
    "artifactSha256": sys.argv[3],
    "sourceTreeSha256": sys.argv[4],
    "releaseEvidence": Path(sys.argv[5]).name,
    "releaseEvidenceSha256": sys.argv[6],
    "releaseEvidenceArtifactSha256": sys.argv[3],
}
manifest.write_text(json.dumps(payload), encoding="utf-8")
PY

"$PYTHON_BIN" "$ROOT/scripts/verify-release-bundle.py" \
    "$artifact" "$checksum" "$manifest" "$evidence" >/dev/null

printf 'mutation\n' >> "$artifact"
if "$PYTHON_BIN" "$ROOT/scripts/verify-release-bundle.py" \
    "$artifact" "$checksum" "$manifest" "$evidence" >/dev/null 2>&1; then
    printf 'Verifier accepted a mutated artifact.\n' >&2
    exit 1
fi

printf 'Release evidence self-test passed.\n'
