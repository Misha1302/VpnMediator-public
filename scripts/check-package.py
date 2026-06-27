#!/usr/bin/env python3
"""Validate release-package structure and reject runtime/secrets artifacts."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
from pathlib import Path

REQUIRED = (
    "Program.cs",
    "MediatorStorage.cs",
    "UnifiedSubscriptionFeed.cs",
    "VpnMediator.csproj",
    "global.json",
    "deploy/vpnmediator.service",
    "VpnMediator.Tests/VpnMediator.Tests.csproj",
    "VpnAccessBot/pyproject.toml",
    "VpnAccessBot/requirements.lock",
    "scripts/validate-release.sh",
    "scripts/run-pytest-files-isolated.py",
    "scripts/generate-release-evidence.py",
    "update_vpn_mediator.sh",
    "release/sbom.cdx.json",
    "docs/baseline-report.md",
    "docs/implementation-tracker.md",
    "docs/requirements-traceability.md",
    "docs/release-evidence-matrix.md",
    "docs/release-checklist.md",
    "docs/production-go-no-go.md",
    "docs/known-limitations.md",
    "docs/security-model.md",
    "docs/credential-lifecycle.md",
    "docs/expiration-policy.md",
    "docs/catalog-model.md",
    "docs/deployment-guide.md",
    "docs/backup-restore-runbook.md",
    "docs/incident-response.md",
    "docs/load-test-report.md",
    "docs/advertising-readiness.md",
    "docs/usability-test-plan.md",
    "docs/external-evidence-required.md",
    "docs/multi-bot.md",
    "release/current-release-evidence.md",
    "release/release-gate-results.json",
    "release/source-tree.sha256",
    "release/build-metadata.txt",
    "release/package-metadata.txt",
)
FORBIDDEN_DIR_NAMES = {
    ".git",
    ".venv",
    "__pycache__",
    ".pytest_cache",
    ".ruff_cache",
    "bin",
    "obj",
    "backups",
    "build",
    "logs",
}
FORBIDDEN_SUFFIXES = {
    ".db",
    ".sqlite",
    ".sqlite3",
    ".wal",
    ".shm",
    ".pfx",
    ".p12",
    ".key",
}
FORBIDDEN_FILE_NAMES = {".env"}


HASH_EXCLUDED_FILE_NAMES = {
    ".env",
    "current-release-evidence.md",
    "source-tree.sha256",
    "build-metadata.txt",
    "package-metadata.txt",
}


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _source_tree_sha(root: Path) -> str:
    aggregate = hashlib.sha256()
    for current_root, directories, files in os.walk(root):
        directories[:] = sorted(name for name in directories if name not in FORBIDDEN_DIR_NAMES)
        current = Path(current_root)
        for name in sorted(files):
            path = current / name
            relative = path.relative_to(root)
            if name in HASH_EXCLUDED_FILE_NAMES or path.suffix.lower() in FORBIDDEN_SUFFIXES:
                continue
            if relative.as_posix().startswith("release/release-gate-results"):
                continue
            aggregate.update(relative.as_posix().encode("utf-8"))
            aggregate.update(b"\0")
            aggregate.update(bytes.fromhex(_sha256(path)))
            aggregate.update(b"\n")
    return aggregate.hexdigest()


def _required_evidence_value(text: str, label: str) -> str:
    match = re.search(rf"^- {re.escape(label)}: `([^`]+)`$", text, re.MULTILINE)
    if match is None:
        fail(f"release evidence is missing {label}")
    return match.group(1)


def _migration_versions(root: Path) -> tuple[int, int]:
    bot_text = (root / "VpnAccessBot/vpn_access_bot/migrations.py").read_text(encoding="utf-8")
    bot_versions = [
        int(value) for value in re.findall(r"_is_applied\(connection,\s*(\d+)\)", bot_text)
    ]
    mediator_text = (root / "MediatorStorage.cs").read_text(encoding="utf-8")
    mediator_match = re.search(r"CurrentMigrationVersion\s*=\s*(\d+)", mediator_text)
    if not bot_versions or mediator_match is None:
        fail("could not determine migration versions from source")
    return max(bot_versions), int(mediator_match.group(1))


def fail(message: str) -> None:
    print(f"Package validation failed: {message}", file=sys.stderr)
    raise SystemExit(1)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("root", nargs="?", default=".")
    args = parser.parse_args()
    root = Path(args.root).resolve()
    if not root.is_dir():
        fail(f"not a directory: {root}")

    missing = [item for item in REQUIRED if not (root / item).is_file()]
    if missing:
        fail("missing required files: " + ", ".join(missing))

    forbidden_compatibility_files = [
        item
        for item in (
            "deploy/vpn-mediator.service",
            "deploy/vpn-probe-agent.service",
            "VpnAccessBot/uv.lock",
            "VpnProbeAgent/pyproject.toml",
            "VpnProvisioning.Contracts/VpnProvisioning.Contracts.csproj",
        )
        if (root / item).exists()
    ]
    if forbidden_compatibility_files:
        fail("obsolete or competing release files: " + ", ".join(forbidden_compatibility_files))

    bad: list[str] = []
    for current_root, directories, files in os.walk(root):
        current = Path(current_root)
        forbidden_directories = [
            name
            for name in directories
            if name in FORBIDDEN_DIR_NAMES or name.endswith(".egg-info")
        ]
        for name in forbidden_directories:
            bad.append(str((current / name).relative_to(root)) + "/")
        directories[:] = [name for name in directories if name not in forbidden_directories]
        for name in files:
            path = current / name
            relative = path.relative_to(root)
            if name in FORBIDDEN_FILE_NAMES or path.suffix.lower() in FORBIDDEN_SUFFIXES:
                bad.append(str(relative))
    if bad:
        fail("forbidden runtime/sensitive artifacts: " + ", ".join(sorted(set(bad))))

    evidence_path = root / "release/current-release-evidence.md"
    evidence = evidence_path.read_text(encoding="utf-8")
    if _required_evidence_value(evidence, "Verdict") != "RELEASE_GATE_PASS":
        fail("release evidence verdict is not RELEASE_GATE_PASS")
    baseline_name = _required_evidence_value(evidence, "Baseline archive")
    baseline_sha = _required_evidence_value(evidence, "Baseline archive SHA-256")
    if baseline_name == "not supplied":
        fail("release evidence does not identify the authoritative baseline archive")
    if re.fullmatch(r"[0-9a-f]{64}", baseline_sha) is None:
        fail("release evidence baseline SHA-256 is invalid")

    for metadata_name in ("build-metadata.txt", "package-metadata.txt"):
        metadata = {}
        for line in (root / "release" / metadata_name).read_text(encoding="utf-8").splitlines():
            if "=" in line:
                key, value = line.split("=", maxsplit=1)
                metadata[key] = value
        if metadata.get("input_archive_filename") != baseline_name:
            fail(f"release/{metadata_name} baseline filename is stale")
        if metadata.get("input_archive_sha256") != baseline_sha:
            fail(f"release/{metadata_name} baseline SHA-256 is stale")

    actual_source_sha = _source_tree_sha(root)
    evidence_source_sha = _required_evidence_value(evidence, "Source tree SHA-256")
    if evidence_source_sha != actual_source_sha:
        fail(
            "release evidence source-tree hash does not match package contents: "
            f"expected {evidence_source_sha}, actual {actual_source_sha}"
        )

    source_sha_path = root / "release/source-tree.sha256"
    source_sha_text = source_sha_path.read_text(encoding="utf-8").strip().split()[0]
    if source_sha_text != actual_source_sha:
        fail("release/source-tree.sha256 does not match package contents")

    bot_version, mediator_version = _migration_versions(root)
    if _required_evidence_value(evidence, "Bot schema version") != str(bot_version):
        fail("release evidence bot schema version is stale")
    if _required_evidence_value(evidence, "Mediator schema version") != str(mediator_version):
        fail("release evidence mediator schema version is stale")

    gate_results = json.loads(
        (root / "release/release-gate-results.json").read_text(encoding="utf-8")
    )
    if not isinstance(gate_results, list) or any(
        not isinstance(item, dict) or item.get("status") == "FAIL" for item in gate_results
    ):
        fail("release gate results contain a failure or invalid entry")

    print(f"Package validation passed: {root}")


if __name__ == "__main__":
    main()
