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

import os
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

def _load_forbidden_terms() -> list[tuple[str, re.Pattern[str]]]:
    """Load forbidden-term patterns from the gitignored local file.

    The terms themselves (internal project names, funding references, internal
    tagging schemes) must never live in the public repository — putting them in
    this scanner's own source would make the leak-prevention tool the leak. So
    they are kept in scripts/forbidden-terms.local (gitignored; one extended
    regex per line, '#' comments and blanks ignored), the same file the shell
    pre-commit hook reads. If the file is absent the forbidden-term scan is
    skipped with a notice; the secret/key scans still run.
    """
    local = os.path.join(os.path.dirname(__file__), "forbidden-terms.local")
    if not os.path.exists(local):
        print(
            f"Notice: {local} not found; skipping forbidden-term scan "
            "(secret/key scans still run). See forbidden-terms.local.example.",
            file=sys.stderr,
        )
        return []
    terms: list[tuple[str, re.Pattern[str]]] = []
    with open(local, encoding="utf-8") as fh:
        for raw in fh:
            line = raw.strip()
            if not line or line.startswith("#"):
                continue
            try:
                terms.append(("internal reference", re.compile(line)))
            except re.error:
                print(f"Warning: bad regex in forbidden-terms.local: {line!r}",
                      file=sys.stderr)
    return terms


FORBIDDEN_TERMS: list[tuple[str, re.Pattern[str]]] = _load_forbidden_terms()

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
