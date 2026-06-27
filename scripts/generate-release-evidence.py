#!/usr/bin/env python3
"""Generate immutable, source-specific release evidence from executed gate results."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import platform
import re
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

EXCLUDED_DIRECTORY_NAMES = {
    ".git",
    ".venv",
    "__pycache__",
    ".pytest_cache",
    ".ruff_cache",
    "bin",
    "obj",
    "logs",
    "backups",
    "build",
}
EXCLUDED_FILE_NAMES = {
    ".env",
    "current-release-evidence.md",
    "source-tree.sha256",
    "build-metadata.txt",
    "package-metadata.txt",
}
EXCLUDED_SUFFIXES = {
    ".db",
    ".sqlite",
    ".sqlite3",
    ".wal",
    ".shm",
    ".pfx",
    ".p12",
    ".key",
}


def _run_version(command: list[str]) -> str:
    try:
        completed = subprocess.run(
            command,
            check=False,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            timeout=30,
        )
    except (OSError, subprocess.TimeoutExpired):
        return "unavailable"
    if completed.returncode != 0:
        return "unavailable"
    return completed.stdout.strip().splitlines()[0] if completed.stdout.strip() else "unknown"


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _iter_source_files(root: Path):
    for current_root, directories, files in os.walk(root):
        directories[:] = sorted(
            name for name in directories if name not in EXCLUDED_DIRECTORY_NAMES
        )
        current = Path(current_root)
        for name in sorted(files):
            path = current / name
            relative = path.relative_to(root)
            if name in EXCLUDED_FILE_NAMES or path.suffix.lower() in EXCLUDED_SUFFIXES:
                continue
            if relative.as_posix().startswith("release/release-gate-results"):
                continue
            yield relative, path


def _source_tree_sha(root: Path) -> str:
    aggregate = hashlib.sha256()
    for relative, path in _iter_source_files(root):
        aggregate.update(relative.as_posix().encode("utf-8"))
        aggregate.update(b"\0")
        aggregate.update(bytes.fromhex(_sha256(path)))
        aggregate.update(b"\n")
    return aggregate.hexdigest()


def _migration_versions(root: Path) -> tuple[int | None, int | None]:
    bot_text = (root / "VpnAccessBot/vpn_access_bot/migrations.py").read_text(encoding="utf-8")
    bot_versions = [
        int(value) for value in re.findall(r"_is_applied\(connection,\s*(\d+)\)", bot_text)
    ]
    mediator_text = (root / "MediatorStorage.cs").read_text(encoding="utf-8")
    mediator_match = re.search(r"CurrentMigrationVersion\s*=\s*(\d+)", mediator_text)
    return (
        max(bot_versions) if bot_versions else None,
        int(mediator_match.group(1)) if mediator_match else None,
    )


def _load_results(path: Path) -> list[dict[str, Any]]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, list):
        raise ValueError("results JSON must be an array")
    required = {"name", "command", "status", "summary"}
    results: list[dict[str, Any]] = []
    for index, item in enumerate(payload):
        if not isinstance(item, dict) or not required <= item.keys():
            raise ValueError(f"result {index} is missing required fields")
        if item["status"] not in {"PASS", "FAIL", "SKIPPED_EXTERNAL"}:
            raise ValueError(f"result {index} has invalid status")
        results.append(item)
    return results


def _escape(value: object) -> str:
    return str(value).replace("|", "\\|").replace("\n", " ")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", default=".")
    baseline = parser.add_mutually_exclusive_group(required=True)
    baseline.add_argument("--baseline-sha")
    baseline.add_argument("--baseline-archive")
    parser.add_argument("--results-json", required=True)
    parser.add_argument("--output", default="release/current-release-evidence.md")
    parser.add_argument("--artifact")
    args = parser.parse_args()

    root = Path(args.root).resolve()
    results_path = Path(args.results_json).resolve()
    output = Path(args.output)
    if not output.is_absolute():
        output = root / output
    artifact = Path(args.artifact).resolve() if args.artifact else None

    if not root.is_dir():
        raise SystemExit(f"repository root does not exist: {root}")
    baseline_archive = None
    if args.baseline_archive:
        baseline_archive = Path(args.baseline_archive).resolve()
        if not baseline_archive.is_file() or baseline_archive.stat().st_size <= 0:
            raise SystemExit(f"baseline archive is missing or empty: {baseline_archive}")
        baseline_sha = _sha256(baseline_archive)
    else:
        baseline_sha = str(args.baseline_sha)
        if not re.fullmatch(r"[0-9a-f]{64}", baseline_sha):
            raise SystemExit("baseline SHA-256 must be 64 lowercase hexadecimal characters")
    results = _load_results(results_path)
    bot_version, mediator_version = _migration_versions(root)
    generated_at = datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")

    artifact_lines = ["- Artifact: not supplied"]
    if artifact is not None:
        if not artifact.is_file() or artifact.stat().st_size <= 0:
            raise SystemExit(f"artifact is missing or empty: {artifact}")
        artifact_lines = [
            f"- Artifact: `{artifact.name}`",
            f"- Artifact size: `{artifact.stat().st_size}` bytes",
            f"- Artifact SHA-256: `{_sha256(artifact)}`",
        ]

    failed = [item for item in results if item["status"] == "FAIL"]
    verdict = "RELEASE_GATE_PASS" if not failed else "RELEASE_GATE_FAIL"
    lines = [
        "# Current release evidence",
        "",
        "> Generated by `scripts/generate-release-evidence.py`. Do not reuse this "
        "file for a different source tree or artifact.",
        "",
        "## Identity",
        "",
        f"- Generated at UTC: `{generated_at}`",
        f"- Baseline archive: `{baseline_archive.name if baseline_archive else 'not supplied'}`",
        f"- Baseline archive SHA-256: `{baseline_sha}`",
        f"- Source tree SHA-256: `{_source_tree_sha(root)}`",
        f"- Verdict: `{verdict}`",
        f"- Bot schema version: `{bot_version}`",
        f"- Mediator schema version: `{mediator_version}`",
        *artifact_lines,
        "",
        "## Toolchain",
        "",
        f"- OS: `{platform.platform()}`",
        f"- Python: `{platform.python_version()}`",
        f"- Ruff: `{_run_version([sys.executable, '-m', 'ruff', '--version'])}`",
        f"- Pytest: `{_run_version([sys.executable, '-m', 'pytest', '--version'])}`",
        f"- .NET: `{_run_version(['dotnet', '--version'])}`",
        f"- ShellCheck: `{_run_version(['shellcheck', '--version'])}`",
        "",
        "## Executed gates",
        "",
        "| Gate | Status | Exact command | Result |",
        "|---|---|---|---|",
    ]
    for item in results:
        lines.append(
            f"| {_escape(item['name'])} | `{_escape(item['status'])}` | "
            f"`{_escape(item['command'])}` | {_escape(item['summary'])} |"
        )

    lines.extend(
        [
            "",
            "## External conditions not proven by this evidence",
            "",
            "- Real Telegram Stars payment and refund delivery.",
            "- Real Happ clients and production VPN core behavior.",
            "- Production DNS, TLS, Nginx, systemd restart, OOM/SIGKILL, "
            "alerts and offsite restore.",
            "- Production deployment or canary behavior.",
            "",
        ]
    )
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text("\n".join(lines), encoding="utf-8")
    print(f"Release evidence generated: {output}")
    print(f"Verdict: {verdict}")
    if failed:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
