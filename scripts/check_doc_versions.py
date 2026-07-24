#!/usr/bin/env python3
"""Fail when a version quoted in the docs no longer matches the code.

The framework docs name the version floor each extra installs, for example
"Requires `crewai>=1.15.5` (installed by the `crewai` extra)". Those numbers
are copied from the pyproject files by hand, so raising a floor silently
leaves every doc that quotes it wrong -- which is how nine of them ended up
a major version behind at once.

This walks every `name>=version` in backticks under docs/ and in the
top-level markdown, looks the package up in the three pyproject files, and
reports any that disagree. Packages the project does not declare are ignored,
so quoting a third party's own pin (crewai pins `chromadb~=1.1.0`) does not
trip it.

Run: python scripts/check_doc_versions.py
"""

from __future__ import annotations

import re
import sys
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PYPROJECTS = ("receiver/pyproject.toml", "sdk/pyproject.toml", "cli/pyproject.toml")
QUOTED = re.compile(r"`([A-Za-z0-9_.\-]+)>=([0-9][^`,\s]*)`")
REQUIREMENT = re.compile(r"([A-Za-z0-9_.\-]+)(\[[^\]]+\])?>=([0-9][^,\s\"]*)")


def declared_floors() -> dict[str, str]:
    """Map every package the project declares to the floor it declares."""
    floors: dict[str, str] = {}
    for rel in PYPROJECTS:
        path = ROOT / rel
        if not path.exists():
            continue
        project = tomllib.loads(path.read_text())["project"]
        requirements = list(project.get("dependencies", []))
        for extra in (project.get("optional-dependencies") or {}).values():
            requirements += extra
        for requirement in requirements:
            match = REQUIREMENT.match(requirement)
            if match:
                floors[match.group(1).lower()] = match.group(3)
    return floors


def main() -> int:
    floors = declared_floors()
    if not floors:
        print("no dependency floors found; is this running from the repo root?")
        return 1

    docs = sorted((ROOT / "docs").rglob("*.md")) + sorted(ROOT.glob("*.md"))

    mismatches = []
    for doc in docs:
        for match in QUOTED.finditer(doc.read_text()):
            name, quoted = match.group(1).lower(), match.group(2)
            declared = floors.get(name)
            if declared is not None and declared != quoted:
                mismatches.append((doc.relative_to(ROOT), name, quoted, declared))

    if mismatches:
        print(f"{len(mismatches)} documented version(s) disagree with the code:\n")
        for doc, name, quoted, declared in mismatches:
            print(f"  {doc}: says {name}>={quoted}, code declares >={declared}")
        print("\nUpdate the doc, or the floor, so the two agree.")
        return 1

    print(
        f"ok: every documented version matches the code "
        f"({len(floors)} packages across {len(docs)} files)"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
