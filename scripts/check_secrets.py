#!/usr/bin/env python3
"""Pre-commit guard: block secret-shaped strings from being committed.

Scans staged content (or, with --all, the whole tree) for secret-shaped
strings — real API keys, tokens, private keys, and high-entropy assignments
that look like credentials — and blocks the commit if any are found.

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
    # Both secret scanners necessarily contain secret-shaped detection patterns
    # and documented example keys (AKIA…EXAMPLE, PEM headers). Each exempts the
    # other so a scanner's own patterns aren't flagged as a leak.
    r"|check_secrets\.py$|check-secrets\.sh$)"
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
    for lineno, line in enumerate(text.splitlines(), start=1):
        if _line_is_allowlisted(line):
            continue
        if not exempt_secrets:
            for label, pat in SECRET_PATTERNS:
                if pat.search(line):
                    findings.append(f"{path}:{lineno}: possible {label}")
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
        print("BLOCKED: potential secrets detected\n",
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
