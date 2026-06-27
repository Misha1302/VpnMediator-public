#!/usr/bin/env python3
from __future__ import annotations

import json
import re
import xml.etree.ElementTree as ET
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
components: list[dict[str, str]] = []
requirements = (ROOT / "VpnAccessBot" / "requirements.lock").read_text(encoding="utf-8")
for line in requirements.splitlines():
    match = re.match(r"^([A-Za-z0-9_.-]+)==([^\\\s]+)\\?", line)
    if match:
        components.append({"type": "library", "name": match.group(1), "version": match.group(2)})
for project in (
    ROOT / "VpnMediator.csproj",
    ROOT / "VpnMediator.Tests" / "VpnMediator.Tests.csproj",
):
    tree = ET.parse(project)
    for reference in tree.findall(".//PackageReference"):
        components.append(
            {
                "type": "library",
                "name": reference.attrib["Include"],
                "version": reference.attrib.get("Version", "unspecified"),
            }
        )
components = sorted(
    {f"{item['name']}@{item['version']}": item for item in components}.values(),
    key=lambda item: (item["name"].lower(), item["version"]),
)
payload = {
    "bomFormat": "CycloneDX",
    "specVersion": "1.5",
    "version": 1,
    "metadata": {"component": {"type": "application", "name": "Razaltush VPN services"}},
    "components": components,
}
output = ROOT / "release" / "sbom.cdx.json"
output.parent.mkdir(parents=True, exist_ok=True)
output.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
print(output)
