#!/usr/bin/env bash
# Install the repo's git hooks. Run once after cloning:
#
#   ./scripts/install-hooks.sh
#
# Wires scripts/check_secrets.py as the pre-commit hook so staged content is
# scanned for secrets and forbidden planning terms before every commit. The
# same scan runs in CI (.github/workflows/ci.yml), so this is local
# defense-in-depth, not the only line.
set -euo pipefail

repo_root="$(git rev-parse --show-toplevel)"
hook="$repo_root/.git/hooks/pre-commit"

# Remove any existing hook first. Critical: never write *through* it with
# `cat >`, because if it is a symlink the redirect would clobber the target.
rm -f "$hook"

cat > "$hook" <<'HOOK'
#!/usr/bin/env bash
# Auto-installed by scripts/install-hooks.sh — scans staged content.
exec python3 "$(git rev-parse --show-toplevel)/scripts/check_secrets.py"
HOOK

chmod +x "$hook"
chmod +x "$repo_root/scripts/check_secrets.py"
echo "Installed pre-commit hook -> scripts/check_secrets.py"
echo "Test it now with: python3 scripts/check_secrets.py --all"
