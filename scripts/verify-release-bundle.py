#!/usr/bin/env python3
from __future__ import annotations

import argparse
import hashlib
import json
import re
from pathlib import Path


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def require_sha(value: object, field: str) -> str:
    text = str(value)
    if re.fullmatch(r"[0-9a-f]{64}", text) is None:
        raise ValueError(f"{field} is not a lowercase SHA-256 value")
    return text


def main() -> int:
    parser = argparse.ArgumentParser(description="Verify an artifact and all release sidecars.")
    parser.add_argument("artifact", type=Path)
    parser.add_argument("checksum", type=Path)
    parser.add_argument("manifest", type=Path)
    parser.add_argument("evidence", type=Path)
    args = parser.parse_args()

    for path in (args.artifact, args.checksum, args.manifest, args.evidence):
        if not path.is_file() or path.stat().st_size <= 0:
            raise SystemExit(f"release bundle file is missing or empty: {path}")

    actual_artifact_sha = sha256(args.artifact)
    checksum_parts = args.checksum.read_text(encoding="utf-8").strip().split()
    if len(checksum_parts) != 2:
        raise SystemExit("checksum sidecar has an invalid format")
    checksum_sha, checksum_name = checksum_parts
    if require_sha(checksum_sha, "checksum SHA") != actual_artifact_sha:
        raise SystemExit("checksum sidecar does not match the artifact")
    if checksum_name != args.artifact.name:
        raise SystemExit("checksum sidecar names a different artifact")

    manifest = json.loads(args.manifest.read_text(encoding="utf-8"))
    if manifest.get("artifact") != args.artifact.name:
        raise SystemExit("manifest names a different artifact")
    if require_sha(manifest.get("artifactSha256"), "manifest artifact SHA") != actual_artifact_sha:
        raise SystemExit("manifest does not match the artifact SHA-256")
    if int(manifest.get("artifactSizeBytes", -1)) != args.artifact.stat().st_size:
        raise SystemExit("manifest does not match the artifact size")
    if manifest.get("releaseEvidence") != args.evidence.name:
        raise SystemExit("manifest names a different release-evidence sidecar")
    if require_sha(manifest.get("releaseEvidenceSha256"), "manifest evidence SHA") != sha256(
        args.evidence
    ):
        raise SystemExit("manifest does not match the release-evidence SHA-256")

    evidence_text = args.evidence.read_text(encoding="utf-8")
    artifact_match = re.search(
        r"^- Artifact SHA-256: `([0-9a-f]{64})`$", evidence_text, re.MULTILINE
    )
    source_match = re.search(
        r"^- Source tree SHA-256: `([0-9a-f]{64})`$", evidence_text, re.MULTILINE
    )
    if artifact_match is None or artifact_match.group(1) != actual_artifact_sha:
        raise SystemExit("release evidence does not match the artifact SHA-256")
    manifest_source_sha = require_sha(manifest.get("sourceTreeSha256"), "source-tree SHA")
    if source_match is None or source_match.group(1) != manifest_source_sha:
        raise SystemExit("release evidence does not match the manifest source-tree SHA-256")
    if manifest.get("releaseEvidenceArtifactSha256") != actual_artifact_sha:
        raise SystemExit("manifest evidence binding does not match the artifact")

    print(f"Verified release bundle: artifact={args.artifact.name} sha256={actual_artifact_sha}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
