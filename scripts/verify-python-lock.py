#!/usr/bin/env python3
"""Validate that Python lock files are hash-pinned and source-neutral."""

from __future__ import annotations

import re
import sys
from pathlib import Path

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
LOCK_PATHS = (
    REPOSITORY_ROOT / "VpnAccessBot" / "requirements.lock",
    REPOSITORY_ROOT / "VpnAccessBot" / "build-requirements.lock",
)
PACKAGE_PATTERN = re.compile(r"^[A-Za-z0-9_.-]+==[^\s\\]+")
FORBIDDEN = (
    "--index-url",
    "--extra-index-url",
    "--find-links",
    "git+",
    "https://",
    "http://",
    "-e ",
)


def fail(message: str) -> None:
    print(f"Python lock verification failed: {message}", file=sys.stderr)
    raise SystemExit(1)


def validate_lock(lock_path: Path) -> int:
    if not lock_path.is_file():
        fail(f"missing {lock_path}")

    text = lock_path.read_text(encoding="utf-8")
    lowered = text.lower()
    for marker in FORBIDDEN:
        if marker in lowered:
            fail(f"{lock_path.name}: forbidden source marker {marker!r}")

    groups: list[list[str]] = []
    current: list[str] = []
    for raw_line in text.splitlines():
        stripped = raw_line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        if PACKAGE_PATTERN.match(stripped):
            if current:
                groups.append(current)
            current = [stripped]
        elif current:
            current.append(stripped)
        else:
            fail(f"{lock_path.name}: unexpected content before first requirement: {stripped!r}")
    if current:
        groups.append(current)

    if not groups:
        fail(f"{lock_path.name}: contains no pinned requirements")

    missing_hash = [
        group[0] for group in groups if not any("--hash=sha256:" in line for line in group)
    ]
    if missing_hash:
        fail(f"{lock_path.name}: requirements without SHA-256 hashes: " + ", ".join(missing_hash))

    print(f"Python lock verified: {lock_path.name} ({len(groups)} hash-pinned packages).")
    return len(groups)


def main() -> None:
    total = sum(validate_lock(lock_path) for lock_path in LOCK_PATHS)
    print(f"Python lock verification passed: {total} hash-pinned packages in total.")


if __name__ == "__main__":
    main()
