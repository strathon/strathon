#!/usr/bin/env bash
# Pin every GitHub Action in .github/workflows/*.yml to the exact commit SHA its
# tag currently points to, then append the tag as a comment (supply-chain
# hardening). Tags are mutable -- a compromised action release could be
# re-tagged to point at malicious code; a 40-char SHA cannot be moved.
#
# Run from the repo root. Requires: gh CLI authenticated (`gh auth status`) OR a
# GITHUB_TOKEN env var. Resolves the REAL SHA via the API -- never guesses.
#
#   ./scripts/pin_actions_to_sha.sh          # rewrite in place
#   ./scripts/pin_actions_to_sha.sh --check  # list what would change, exit 1 if any
set -euo pipefail

CHECK=false
[[ "${1:-}" == "--check" ]] && CHECK=true

# Resolve owner/repo@ref -> commit SHA (dereferencing annotated tags).
resolve_sha() {
  local repo="$1" ref="$2"
  # /commits/<ref> follows tags (annotated or lightweight) to the commit SHA.
  if command -v gh >/dev/null 2>&1; then
    gh api "repos/${repo}/commits/${ref}" --jq '.sha' 2>/dev/null
  else
    curl -sf -H "Authorization: Bearer ${GITHUB_TOKEN}" \
      "https://api.github.com/repos/${repo}/commits/${ref}" |
      python3 -c 'import sys,json;print(json.load(sys.stdin)["sha"])'
  fi
}

changed=0
# Collect every distinct `uses: owner/repo@ref` still on a tag (not already a SHA).
mapfile -t uses < <(grep -rhoE 'uses: [^ ]+@[^ ]+' .github/workflows/*.yml \
  | sed 's/uses: //' | sort -u | grep -vE '@[0-9a-f]{40}$')

for u in "${uses[@]}"; do
  repo="${u%@*}"; ref="${u#*@}"
  # Skip local (./) and docker:// actions.
  [[ "$repo" == ./* || "$repo" == docker://* ]] && continue
  sha="$(resolve_sha "$repo" "$ref")" || { echo "WARN: could not resolve $u" >&2; continue; }
  [[ -z "$sha" ]] && { echo "WARN: empty SHA for $u" >&2; continue; }
  echo "  $repo@$ref -> $sha"
  changed=1
  $CHECK && continue
  # Rewrite `uses: repo@ref` -> `uses: repo@<sha> # ref` across all workflows.
  # Escape / in repo for sed.
  esc_repo="${repo//\//\\/}"
  find .github/workflows -name '*.yml' -print0 | xargs -0 sed -i \
    "s/uses: ${esc_repo}@${ref}\b.*/uses: ${esc_repo}@${sha} # ${ref}/"
done

if $CHECK; then
  [[ "$changed" == "1" ]] && { echo "Unpinned actions found (run without --check to pin)."; exit 1; }
  echo "All actions are SHA-pinned."; exit 0
fi
echo "Done. Review the diff, then commit."
