#!/usr/bin/env python3
"""Pre-commit guard: block secrets and private planning terms from being
committed to the public repository.

Scans staged content (or, with --all, the whole tree) for two classes of
problem:

  1. Secret-shaped strings — real API keys, tokens, private keys, and
     high-entropy assignments that look like credentials.
  2. Forbidden terms — internal planning/competitive context that must never
     appear in public artifacts: roadmap sequencing, investor/accelerator
     references, the sibling project name, and internal commit-stage tags.

Dependency-free (stdlib only) so it runs as a git hook without a framework.
Exit non-zero on any finding so the commit (or CI job) fails.

Install via:  ./scripts/install-hooks.sh
Run over all tracked files:  python scripts/check_secrets.py --all
"""

from __future__ import annotations

import re
import subprocess
import sys

SECRET_PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    ("AWS access key id", re.compile(r"\bAKIA[0-9A-Z]{16}\b")),
    ("GitHub token", re.compile(r"\bghp_[A-Za-z0-9]{36}\b")),
    ("GitHub fine-grained token", re.compile(r"\bgithub_pat_[A-Za-z0-9_]{22,}\b")),
    ("Slack token", re.compile(r"\bxox[baprs]-[A-Za-z0-9-]{10,}\b")),
    ("OpenAI key", re.compile(r"\bsk-[A-Za-z0-9]{32,}\b")),
    ("Anthropic key", re.compile(r"\bsk-ant-[A-Za-z0-9_-]{20,}\b")),
    ("Google API key", re.compile(r"\bAIza[0-9A-Za-z_-]{35}\b")),
    ("Stripe live key", re.compile(r"\bsk_live_[0-9a-zA-Z]{24,}\b")),
    ("PEM private key", re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----")),
    ("Generic bearer secret assignment",
     re.compile(r"""(?i)(?:api[_-]?key|secret|token|passwd|password)\s*[:=]\s*['"][A-Za-z0-9/+_-]{24,}['"]""")),
]

FORBIDDEN_TERMS: list[tuple[str, re.Pattern[str]]] = [
    ("sibling project name", re.compile(r"\bStrathos\b")),
    ("investor/accelerator (Emergent Ventures)", re.compile(r"\bEmergent\s+Ventures\b", re.I)),
    ("accelerator (YC / Y Combinator)", re.compile(r"\bY\s*Combinator\b|\bYC\s*[WS]\d{2}\b")),
    ("internal roadmap reference", re.compile(r"\broadmap\.md\b")),
    ("internal commit-stage tag",
     re.compile(r"\b(?:commit\s+[CHP]\d|stage\s+\d|[CHP]\d-[A-Za-z])")),
]

ALLOWLIST_SUBSTRINGS = (
    "stra_dev_local_default_project_do_not_use_in_production",
    "CHANGE_ME",
    "your_api_key",
    "your-api-key",
    "example.com",
)

EXEMPT_PATH_RE = re.compile(
    r"(credential_patterns\.py$"
    r"|/tests?/"
    r"|\.lock$|lock\.json$|\.lockb$"
    r"|check_secrets\.py$)"
)


def _staged_files() -> list[str]:
    out = subprocess.run(
        ["git", "diff", "--cached", "--name-only", "--diff-filter=ACM"],
        capture_output=True, text=True, check=False,
    )
    return [f for f in out.stdout.splitlines() if f.strip()]


def _all_tracked_files() -> list[str]:
    out = subprocess.run(
        ["git", "ls-files"], capture_output=True, text=True, check=False
    )
    return [f for f in out.stdout.splitlines() if f.strip()]


def _read(path: str) -> str | None:
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as fh:
            return fh.read()
    except (OSError, IsADirectoryError):
        return None


def _line_is_allowlisted(line: str) -> bool:
    return any(s in line for s in ALLOWLIST_SUBSTRINGS)


def scan_file(path: str) -> list[str]:
    findings: list[str] = []
    text = _read(path)
    if text is None:
        return findings
    exempt_secrets = bool(EXEMPT_PATH_RE.search(path))
    # The scanner's own source defines the forbidden-term patterns, so it
    # necessarily contains those terms. Exempt it from the forbidden-term
    # check (but not from real secret scanning).
    exempt_forbidden = path.endswith("check_secrets.py") or path.endswith(".gitignore")
    for lineno, line in enumerate(text.splitlines(), start=1):
        if _line_is_allowlisted(line):
            continue
        if not exempt_secrets:
            for label, pat in SECRET_PATTERNS:
                if pat.search(line):
                    findings.append(f"{path}:{lineno}: possible {label}")
        if not exempt_forbidden:
            for label, pat in FORBIDDEN_TERMS:
                if pat.search(line):
                    findings.append(f"{path}:{lineno}: forbidden term — {label}")
    return findings


def main() -> int:
    scan_all = "--all" in sys.argv
    files = _all_tracked_files() if scan_all else _staged_files()
    skip_ext = (".png", ".jpg", ".jpeg", ".gif", ".webp", ".ico", ".pdf",
                ".zip", ".gz", ".whl", ".woff", ".woff2", ".ttf")
    files = [f for f in files if not f.lower().endswith(skip_ext)]

    all_findings: list[str] = []
    for f in files:
        all_findings.extend(scan_file(f))

    if all_findings:
        print("BLOCKED: potential secrets or forbidden terms detected\n",
              file=sys.stderr)
        for finding in all_findings:
            print(f"  {finding}", file=sys.stderr)
        print(
            "\nIf a match is a false positive, add a precise allowlist entry "
            "in scripts/check_secrets.py (ALLOWLIST_SUBSTRINGS) or exempt the "
            "path — do not weaken the patterns.",
            file=sys.stderr,
        )
        return 1
    if scan_all:
        print(f"OK: scanned {len(files)} tracked files, no findings.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
