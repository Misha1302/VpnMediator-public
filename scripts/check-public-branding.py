#!/usr/bin/env python3
"""Reject stale public RazakovVpnBot branding while preserving legacy routing identity."""

from __future__ import annotations

import sys
from pathlib import Path

STALE_PUBLIC_USERNAME = "RazakovVpnBot"
CANONICAL_PUBLIC_USERNAME = "@RazaltushVpnBot"


def fail(message: str) -> None:
    print(f"Public branding guard failed: {message}", file=sys.stderr)
    raise SystemExit(1)


def main() -> None:
    root = Path(__file__).resolve().parent.parent
    public_files = [
        root / "Program.cs",
        root / "appsettings.json",
        root / "deploy/mediator.env.example",
        root / "VpnAccessBot/.env.example",
        root / "VpnAccessBot/deploy/bot.env.example",
        root / "deploy/bot.env.example",
        root / "VpnAccessBot/README.md",
        root / "VpnAccessBot/vpn_access_bot/admin_texts.py",
        root / "VpnAccessBot/vpn_access_bot/error_texts.py",
        root / "VpnAccessBot/vpn_access_bot/home.py",
        root / "VpnAccessBot/vpn_access_bot/keyboards.py",
        root / "VpnAccessBot/vpn_access_bot/texts.py",
        root / "VpnAccessBot/vpn_access_bot/user_texts.py",
        *sorted((root / "VpnAccessBot/vpn_access_bot/handlers").glob("*.py")),
    ]
    stale_locations: list[str] = []
    for path in public_files:
        text = path.read_text(encoding="utf-8")
        for line_number, line in enumerate(text.splitlines(), start=1):
            if STALE_PUBLIC_USERNAME in line:
                stale_locations.append(f"{path.relative_to(root)}:{line_number}")
    if stale_locations:
        fail("stale username found in public surfaces: " + ", ".join(stale_locations))

    canonical_defaults = {
        "appsettings.json": root / "appsettings.json",
        "deploy/mediator.env.example": root / "deploy/mediator.env.example",
        "VpnAccessBot/.env.example": root / "VpnAccessBot/.env.example",
        "VpnAccessBot/deploy/bot.env.example": (root / "VpnAccessBot/deploy/bot.env.example"),
        "deploy/bot.env.example": root / "deploy/bot.env.example",
    }
    missing = [
        name
        for name, path in canonical_defaults.items()
        if CANONICAL_PUBLIC_USERNAME not in path.read_text(encoding="utf-8")
    ]
    if missing:
        fail("canonical public username missing from defaults: " + ", ".join(missing))

    print("Public branding guard passed; legacy Razakov bot keys remain outside public surfaces.")


if __name__ == "__main__":
    main()
