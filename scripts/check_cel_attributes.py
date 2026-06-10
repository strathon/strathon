#!/usr/bin/env python3
"""Guard against CEL attribute drift.

A policy's match expression evaluates CEL against the attributes the engine
emits onto a span. If a doc example or a shipped policy template references an
attribute the engine never emits, a user who copies that policy gets a rule that
silently never matches — the same false-confidence failure class as a firewall
that silently allows. Nothing structurally forced docs/templates to agree with
the emitted attribute set, so the agreement drifted repeatedly.

This script forces the agreement. It:
  1. Collects every attribute the engine EMITS — the ``gen_ai.*`` / ``strathon.*``
     / framework-namespaced string literals in the SDK instrumentation and the
     receiver enforcement surfaces (gateway, egress).
  2. Collects every attribute USED in an ``attrs["..."]`` reference across the
     docs and the shipped policy templates.
  3. Fails if any used attribute is not in the emitted set.

Run from the repo root: ``python scripts/check_cel_attributes.py``
Exit 0 = every referenced attribute is real. Exit 1 = drift found.

Notes / known limits (intentional, documented so a maintainer isn't surprised):
  - Emitted attributes are discovered by string-literal scan, not by running the
    engine. An attribute built by string concatenation would be missed by the
    emitter scan; keep emitted attribute keys as plain string literals.
  - Namespaces checked: gen_ai.*, strathon.*, and the per-framework prefixes.
    A used attribute outside these namespaces is reported so it can't hide.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent

# Directories whose Python files EMIT attributes onto spans.
EMIT_DIRS = [
    REPO / "sdk" / "src" / "strathon",
    REPO / "receiver",
]
# Files/dirs whose content USES attributes in policy expressions.
USE_DOCS = REPO / "docs"
USE_TEMPLATES = REPO / "receiver" / "policy_templates.py"
# The root README and the package READMEs carry headline policy examples that a
# user is especially likely to copy, so they're governed too.
USE_READMES = [REPO / "README.md", REPO / "sdk" / "README.md", REPO / "cli" / "README.md"]

# Attribute namespaces we govern. A used attribute must be in one of these and
# must appear in the emitted set.
NAMESPACES = ("gen_ai.", "strathon.")
# Framework-specific prefixes are legitimately framework-only; a used attribute
# under these is allowed as long as it's emitted by that framework's adapter
# (the emitted scan already covers adapters, so they're treated the same way).
FRAMEWORK_PREFIXES = (
    "crewai.", "autogen.", "langgraph.", "langchain.",
    "pydantic_ai.", "google_adk.", "openai_agents.", "framework.",
)

# A string literal that looks like an attribute key.
_EMIT_LITERAL = re.compile(
    r'"((?:gen_ai|strathon|crewai|autogen|langgraph|langchain|'
    r'pydantic_ai|google_adk|openai_agents|framework)\.[a-z0-9_.]+)"'
)
# An attrs["..."] reference in docs or templates.
_USE_REF = re.compile(r'attrs\[\s*"([a-z0-9_.]+)"\s*\]')


def _iter_py(root: Path):
    for p in root.rglob("*.py"):
        # Tests emit synthetic attrs and assert on absent keys; they are not the
        # emitted contract, so exclude them from the emitted set.
        if "test" in p.name or "/tests/" in str(p):
            continue
        yield p


def collect_emitted() -> set[str]:
    emitted: set[str] = set()
    for root in EMIT_DIRS:
        if not root.exists():
            continue
        for p in _iter_py(root):
            text = p.read_text(encoding="utf-8", errors="ignore")
            emitted.update(_EMIT_LITERAL.findall(text))
    return emitted


def collect_used() -> dict[str, list[str]]:
    """Return {attribute: [source files it's referenced in]}."""
    used: dict[str, list[str]] = {}
    sources: list[Path] = []
    if USE_DOCS.exists():
        sources.extend(USE_DOCS.rglob("*.md"))
    if USE_TEMPLATES.exists():
        sources.append(USE_TEMPLATES)
    for readme in USE_READMES:
        if readme.exists():
            sources.append(readme)
    for p in sources:
        text = p.read_text(encoding="utf-8", errors="ignore")
        for attr in _USE_REF.findall(text):
            used.setdefault(attr, []).append(str(p.relative_to(REPO)))
    return used


def main() -> int:
    emitted = collect_emitted()
    used = collect_used()

    if not emitted:
        print("ERROR: found no emitted attributes — scan is broken, refusing "
              "to pass (a broken checker that passes is worse than no checker).")
        return 1

    problems: list[str] = []
    for attr, files in sorted(used.items()):
        in_ns = attr.startswith(NAMESPACES) or attr.startswith(FRAMEWORK_PREFIXES)
        if not in_ns:
            problems.append(
                f"  {attr!r} (used in {', '.join(sorted(set(files)))}) is not in "
                f"a governed namespace — typo, or a new namespace to register?"
            )
            continue
        if attr not in emitted:
            problems.append(
                f"  {attr!r} (used in {', '.join(sorted(set(files)))}) is NOT "
                f"emitted by the engine — a policy using it silently never "
                f"matches. Fix the reference, or emit the attribute."
            )

    if problems:
        print("CEL attribute drift detected — referenced attributes the engine "
              "does not emit:\n")
        print("\n".join(problems))
        print(f"\n{len(problems)} problem(s). Emitted attribute count: "
              f"{len(emitted)}. Referenced: {len(used)}.")
        return 1

    print(f"OK: all {len(used)} referenced CEL attributes are emitted by the "
          f"engine (emitted set: {len(emitted)}).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
