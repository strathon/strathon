#!/usr/bin/env bash
# scripts/check-secrets.sh — Pre-commit hook for Strathon.
#
# Scans staged files for:
#   1. Real API keys (stra_ followed by 30+ chars, excluding the dev key)
#   2. Private key material (PEM headers, PKCS markers)
#   3. Hardcoded secrets (password=, secret=, hmac_key= with values)
#   4. Common cloud provider keys (AWS, GCP service account JSON)
#
# Install as git hook:
#   cp scripts/check-secrets.sh .git/hooks/pre-commit
#   chmod +x .git/hooks/pre-commit
#
# Or run manually:
#   bash scripts/check-secrets.sh
#
# Exit 0 = clean, Exit 1 = blocked.

set -euo pipefail

FAIL=0

# Get staged files (only added/modified, skip deleted).
STAGED=$(git diff --cached --name-only --diff-filter=ACM 2>/dev/null || true)
if [ -z "$STAGED" ]; then
    exit 0
fi

# Helper: grep staged content (not working tree) for a pattern.
check_pattern() {
    local pattern="$1"
    local description="$2"
    local exclude_pattern="${3:-}"

    for file in $STAGED; do
        # Skip binary files, the hook script itself, test files (which
        # legitimately contain example/placeholder secrets like AWS's
        # documented AKIAIOSFODNN7EXAMPLE key used to test detection), and
        # credential_patterns.py (which DEFINES the detection patterns, e.g.
        # the PEM "-----BEGIN ... PRIVATE KEY-----" headers, as string
        # literals — so it necessarily contains the very patterns this scanner
        # searches for). The Python scanner exempts the same file.
        if [[ "$file" == *.png ]] || [[ "$file" == *.jpg ]] || \
           [[ "$file" == *.ico ]] || [[ "$file" == *.woff* ]] || \
           [[ "$file" == *.zip ]] || [[ "$file" == *.tar.gz ]] || \
           [[ "$file" == *test_* ]] || [[ "$file" == */tests/* ]] || \
           [[ "$file" == *credential_patterns.py ]] || \
           [[ "$file" == "scripts/check-secrets.sh" ]]; then
            continue
        fi

        local content
        content=$(git show ":$file" 2>/dev/null || true)
        if [ -z "$content" ]; then
            continue
        fi

        local matches
        # Use `grep -e PATTERN` so patterns that begin with '-' (e.g. PEM
        # headers like -----BEGIN PRIVATE KEY-----) are treated as a pattern
        # and not as command-line flags. BSD/macOS grep otherwise misparses
        # them even with -nE present.
        if [ -n "$exclude_pattern" ]; then
            matches=$(echo "$content" | grep -nE -e "$pattern" | grep -vE -e "$exclude_pattern" || true)
        else
            matches=$(echo "$content" | grep -nE -e "$pattern" || true)
        fi

        if [ -n "$matches" ]; then
            echo "BLOCKED: $description"
            echo "  File: $file"
            echo "$matches" | head -3 | while read -r line; do
                echo "  $line"
            done
            echo ""
            FAIL=1
        fi
    done
}

echo "Running Strathon secret scan..."

# 1. Real API keys: stra_ followed by 30+ non-whitespace chars.
#    Exclude the well-known dev key used in tests and migrations.
check_pattern \
    'stra_[A-Za-z0-9_]{30,}' \
    "Possible real API key (stra_ prefix with 30+ chars)" \
    "stra_dev_local_default_project_do_not_use_in_production"

# 2. Private key material.
check_pattern \
    '-----BEGIN (RSA |EC |DSA |OPENSSH )?PRIVATE KEY-----' \
    "Private key material (PEM header)"

check_pattern \
    '-----BEGIN CERTIFICATE-----' \
    "Certificate material (consider using a secret store)"

# 3. Hardcoded secrets in non-.env files.
#    Match: password = "...", secret_key = "...", hmac_key = "..." etc.
#    Exclude .env files (those are gitignored), test files (may have test values),
#    and documentation/comments.
for file in $STAGED; do
    if [[ "$file" == *.env* ]] || [[ "$file" == *test_* ]] || \
       [[ "$file" == *.md ]] || [[ "$file" == "scripts/check-secrets.sh" ]]; then
        continue
    fi

    content=$(git show ":$file" 2>/dev/null || true)
    if [ -z "$content" ]; then
        continue
    fi

    # Match: variable = "actual_value" (not empty, not placeholder).
    matches=$(echo "$content" | grep -nEi \
        '(password|secret_key|hmac_key|signing_key|private_key|api_secret)\s*=\s*["\x27][^"\x27]{8,}["\x27]' \
        | grep -viE '(os\.environ|os\.getenv|config\.|settings\.|#|example|placeholder|changeme|your_)' \
        || true)

    if [ -n "$matches" ]; then
        echo "BLOCKED: Possible hardcoded secret"
        echo "  File: $file"
        echo "$matches" | head -3 | while read -r line; do
            echo "  $line"
        done
        echo ""
        FAIL=1
    fi
done

# 4. AWS keys, GCP service account JSON.
check_pattern \
    'AKIA[0-9A-Z]{16}' \
    "Possible AWS access key"

check_pattern \
    '"type"\s*:\s*"service_account"' \
    "Possible GCP service account JSON"

if [ $FAIL -ne 0 ]; then
    echo "=========================================="
    echo "Commit blocked by scripts/check-secrets.sh"
    echo "Fix the issues above or use --no-verify to bypass (not recommended)."
    echo "=========================================="
    exit 1
fi

echo "Secret scan passed."
exit 0
