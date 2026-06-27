#!/usr/bin/env bash

set -Eeuo pipefail
umask 077

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PYTHON_BIN="${PYTHON_BIN:-python3}"
BASELINE_ARCHIVE=""
RESULTS_JSON=""
OUTPUT_DIR=""
PACKAGE_BASENAME=""

usage()
{
    cat <<'EOF'
Usage:
  scripts/build-release-package.sh \
    --baseline-archive PATH \
    --results-json PATH \
    --output-dir PATH \
    --package-basename NAME

The baseline archive and executed gate results are mandatory. The script creates
external .sha256, JSON manifest, and artifact-bound release evidence sidecars.
EOF
}

while [[ "$#" -gt 0 ]]; do
    case "$1" in
        --baseline-archive)
            BASELINE_ARCHIVE="${2:-}"
            shift 2
            ;;
        --results-json)
            RESULTS_JSON="${2:-}"
            shift 2
            ;;
        --output-dir)
            OUTPUT_DIR="${2:-}"
            shift 2
            ;;
        --package-basename)
            PACKAGE_BASENAME="${2:-}"
            shift 2
            ;;
        --help|-h)
            usage
            exit 0
            ;;
        *)
            printf 'Unknown argument: %s\n' "$1" >&2
            usage >&2
            exit 2
            ;;
    esac
done

[[ -n "$BASELINE_ARCHIVE" && -n "$RESULTS_JSON" && -n "$OUTPUT_DIR" && -n "$PACKAGE_BASENAME" ]] || {
    usage >&2
    exit 2
}
[[ "$PACKAGE_BASENAME" =~ ^[A-Za-z0-9._-]+$ ]] || {
    printf 'Package basename contains unsupported characters: %s\n' "$PACKAGE_BASENAME" >&2
    exit 2
}

BASELINE_ARCHIVE="$(realpath "$BASELINE_ARCHIVE")"
RESULTS_JSON="$(realpath "$RESULTS_JSON")"
mkdir -p "$OUTPUT_DIR"
OUTPUT_DIR="$(realpath "$OUTPUT_DIR")"

[[ -s "$BASELINE_ARCHIVE" ]] || {
    printf 'Baseline archive is missing or empty: %s\n' "$BASELINE_ARCHIVE" >&2
    exit 1
}
[[ -s "$RESULTS_JSON" ]] || {
    printf 'Gate results are missing or empty: %s\n' "$RESULTS_JSON" >&2
    exit 1
}
[[ "$OUTPUT_DIR" != "$ROOT" ]] || {
    printf 'Output directory must be outside the repository root.\n' >&2
    exit 1
}

ARTIFACT="$OUTPUT_DIR/$PACKAGE_BASENAME.tar.gz"
CHECKSUM="$ARTIFACT.sha256"
MANIFEST="$ARTIFACT.manifest.json"
EVIDENCE="$ARTIFACT.release-evidence.md"
BASELINE_SHA256="$(sha256sum "$BASELINE_ARCHIVE" | awk '{print $1}')"

staging="$(mktemp -d /tmp/vpnmediator-package.XXXXXX)"
package_complete=0
cleanup()
{
    rm -rf -- "$staging"
    if [[ "$package_complete" != "1" ]]; then
        rm -f -- "$ARTIFACT" "$CHECKSUM" "$MANIFEST" "$EVIDENCE"
    fi
}
trap cleanup EXIT

package_root="$staging/$PACKAGE_BASENAME"
mkdir -p "$package_root"

cd "$ROOT"
tar \
    --exclude='.git' \
    --exclude='.venv' \
    --exclude='__pycache__' \
    --exclude='.pytest_cache' \
    --exclude='.ruff_cache' \
    --exclude='*.egg-info' \
    --exclude='bin' \
    --exclude='obj' \
    --exclude='logs' \
    --exclude='backups' \
    --exclude='build' \
    --exclude='.env' \
    --exclude='*.db' \
    --exclude='*.sqlite' \
    --exclude='*.sqlite3' \
    --exclude='*.wal' \
    --exclude='*.shm' \
    --exclude='*.pfx' \
    --exclude='*.p12' \
    --exclude='*.key' \
    -cf - . | tar -xf - -C "$package_root"

rm -f -- \
    "$package_root/release/current-release-evidence.md" \
    "$package_root/release/source-tree.sha256" \
    "$package_root/release/release-gate-results.json" \
    "$package_root/release/build-metadata.txt" \
    "$package_root/release/package-metadata.txt"
cp -- "$RESULTS_JSON" "$package_root/release/release-gate-results.json"

"$PYTHON_BIN" - "$package_root" "$BASELINE_ARCHIVE" "$BASELINE_SHA256" <<'PY'
from __future__ import annotations

import json
import re
import sys
from datetime import UTC, datetime
from pathlib import Path

root = Path(sys.argv[1])
baseline = Path(sys.argv[2])
baseline_sha = sys.argv[3]
results = json.loads((root / "release/release-gate-results.json").read_text(encoding="utf-8"))
if not isinstance(results, list) or not results:
    raise SystemExit("gate results must be a non-empty JSON array")
if any(not isinstance(item, dict) or item.get("status") == "FAIL" for item in results):
    raise SystemExit("gate results contain a failure or invalid entry")

bot_text = (root / "VpnAccessBot/vpn_access_bot/migrations.py").read_text(encoding="utf-8")
bot_versions = [int(value) for value in re.findall(r"_is_applied\(connection,\s*(\d+)\)", bot_text)]
mediator_text = (root / "MediatorStorage.cs").read_text(encoding="utf-8")
mediator_match = re.search(r"CurrentMigrationVersion\s*=\s*(\d+)", mediator_text)
if not bot_versions or mediator_match is None:
    raise SystemExit("could not determine schema versions")

generated_at = datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")
metadata = {
    "generated_at_utc": generated_at,
    "input_archive_filename": baseline.name,
    "input_archive_sha256": baseline_sha,
    "bot_schema_version": max(bot_versions),
    "mediator_schema_version": int(mediator_match.group(1)),
    "gate_count": len(results),
    "gate_statuses": ",".join(str(item.get("status", "UNKNOWN")) for item in results),
}
text = "".join(f"{key}={value}\n" for key, value in metadata.items())
(root / "release/build-metadata.txt").write_text(text, encoding="utf-8")
(root / "release/package-metadata.txt").write_text(text, encoding="utf-8")
PY

"$PYTHON_BIN" "$package_root/scripts/generate-release-evidence.py" \
    --root "$package_root" \
    --baseline-archive "$BASELINE_ARCHIVE" \
    --results-json "$package_root/release/release-gate-results.json" \
    --output "$package_root/release/current-release-evidence.md"

source_sha="$("$PYTHON_BIN" - "$package_root/release/current-release-evidence.md" <<'PY'
import re
import sys
from pathlib import Path

text = Path(sys.argv[1]).read_text(encoding="utf-8")
match = re.search(r"^- Source tree SHA-256: `([0-9a-f]{64})`$", text, re.MULTILINE)
if match is None:
    raise SystemExit(1)
print(match.group(1))
PY
)"
[[ "$source_sha" =~ ^[0-9a-f]{64}$ ]] || {
    printf 'Could not extract source-tree SHA-256 from generated evidence.\n' >&2
    exit 1
}
printf '%s  source-tree\n' "$source_sha" > "$package_root/release/source-tree.sha256"

"$PYTHON_BIN" "$package_root/scripts/check-package.py" "$package_root"
rm -f -- "$ARTIFACT" "$CHECKSUM" "$MANIFEST" "$EVIDENCE"
tar -C "$staging" -czf "$ARTIFACT" "$PACKAGE_BASENAME"
[[ -s "$ARTIFACT" ]] || {
    printf 'Package artifact is missing or empty: %s\n' "$ARTIFACT" >&2
    exit 1
}
artifact_sha256="$(sha256sum "$ARTIFACT" | awk '{print $1}')"
printf '%s  %s\n' "$artifact_sha256" "$(basename "$ARTIFACT")" > "$CHECKSUM"

"$PYTHON_BIN" - "$MANIFEST" "$ARTIFACT" "$artifact_sha256" "$BASELINE_ARCHIVE" \
    "$BASELINE_SHA256" "$source_sha" <<'PY'
from __future__ import annotations

import json
import sys
from datetime import UTC, datetime
from pathlib import Path

manifest = Path(sys.argv[1])
artifact = Path(sys.argv[2])
payload = {
    "generatedAtUtc": datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z"),
    "artifact": artifact.name,
    "artifactSizeBytes": artifact.stat().st_size,
    "artifactSha256": sys.argv[3],
    "baselineArchive": Path(sys.argv[4]).name,
    "baselineArchiveSha256": sys.argv[5],
    "sourceTreeSha256": sys.argv[6],
}
manifest.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
PY

test_unpack="$staging/unpacked"
mkdir -p "$test_unpack"
tar -xzf "$ARTIFACT" -C "$test_unpack"
"$PYTHON_BIN" "$test_unpack/$PACKAGE_BASENAME/scripts/check-package.py" \
    "$test_unpack/$PACKAGE_BASENAME"
(
    cd "$test_unpack/$PACKAGE_BASENAME"
    SKIP_ONLINE_ADVISORY_CHECKS="${SKIP_ONLINE_ADVISORY_CHECKS:-0}" \
        NUGET_CONFIG_FILE="${NUGET_CONFIG_FILE:-}" \
        VALIDATION_VENV="${VALIDATION_VENV:-}" \
        ./scripts/validate-release.sh
)

"$PYTHON_BIN" "$test_unpack/$PACKAGE_BASENAME/scripts/generate-release-evidence.py" \
    --root "$test_unpack/$PACKAGE_BASENAME" \
    --baseline-archive "$BASELINE_ARCHIVE" \
    --results-json "$test_unpack/$PACKAGE_BASENAME/release/release-gate-results.json" \
    --artifact "$ARTIFACT" \
    --output "$EVIDENCE"

"$PYTHON_BIN" - "$EVIDENCE" "$artifact_sha256" "$source_sha" "$MANIFEST" <<'PY'
from __future__ import annotations

import hashlib
import json
import re
import sys
from pathlib import Path

evidence = Path(sys.argv[1])
expected_artifact_sha = sys.argv[2]
expected_source_sha = sys.argv[3]
manifest = Path(sys.argv[4])
text = evidence.read_text(encoding="utf-8")
artifact_match = re.search(r"^- Artifact SHA-256: `([0-9a-f]{64})`$", text, re.MULTILINE)
source_match = re.search(r"^- Source tree SHA-256: `([0-9a-f]{64})`$", text, re.MULTILINE)
if artifact_match is None or artifact_match.group(1) != expected_artifact_sha:
    raise SystemExit("external release evidence does not match the artifact SHA-256")
if source_match is None or source_match.group(1) != expected_source_sha:
    raise SystemExit("external release evidence does not match the packaged source tree")
evidence_sha = hashlib.sha256(evidence.read_bytes()).hexdigest()
payload = json.loads(manifest.read_text(encoding="utf-8"))
payload["releaseEvidence"] = evidence.name
payload["releaseEvidenceSha256"] = evidence_sha
payload["releaseEvidenceArtifactSha256"] = expected_artifact_sha
manifest.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
PY

"$PYTHON_BIN" "$test_unpack/$PACKAGE_BASENAME/scripts/verify-release-bundle.py" \
    "$ARTIFACT" "$CHECKSUM" "$MANIFEST" "$EVIDENCE"

package_complete=1
printf 'Artifact: %s\n' "$ARTIFACT"
printf 'Checksum: %s\n' "$CHECKSUM"
printf 'Manifest: %s\n' "$MANIFEST"
printf 'Release evidence: %s\n' "$EVIDENCE"
printf 'Artifact SHA-256: %s\n' "$artifact_sha256"
printf 'Source tree SHA-256: %s\n' "$source_sha"
