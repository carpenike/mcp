#!/usr/bin/env bash
#
# pull-contract.sh — update the pinned ref and stage the contract content.
#
# GitHub is the single source of truth for the pocketid-mcp-as contract. We
# do NOT commit contract.json / CONTRACT.md into this source tree. This script
# (a) optionally bumps the pinned ref in contract/PINNED.json, and (b) fetches
# the contract content from upstream@ref into contract/ (gitignored) so dev
# runs + tests have it locally. The wheel build fetches the same way.
#
# Usage:
#   scripts/pull-contract.sh [ref]
#     ref   commit SHA (recommended) or tag/branch to pin
#           (default: keep the current pin, just refetch + re-verify)
#
# Prefer pinning an immutable commit SHA so the pin can't drift under a moved
# tag. Whatever ref you pass, this records the per-file sha256 of the fetched
# content into PINNED.json; hatch_build.py + the build then verify every future
# fetch against those digests, so a repointed tag or tampered content fails.
#
# Bumping the pin is a deliberate, reviewable step: run with a new ref, then
# review the PINNED.json diff and re-run `make conformance-ci`.
#
set -euo pipefail

REPO="carpenike/mcp-as-contract"
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
DEST="${ROOT}/contract"
PINNED="${DEST}/PINNED.json"

for bin in curl jq sha256sum; do
  command -v "$bin" >/dev/null 2>&1 || { echo "missing required tool: $bin" >&2; exit 2; }
done
[[ -f "$PINNED" ]] || { echo "contract/PINNED.json not found" >&2; exit 2; }

REF="${1:-$(jq -r '.ref' "$PINNED")}"
[[ -n "$REF" && "$REF" != "null" ]] || { echo "no ref to pin" >&2; exit 1; }

raw() { curl -fsS "https://raw.githubusercontent.com/${REPO}/${1}/${2}"; }

echo "Fetching ${REPO}@${REF} ..."
mkdir -p "$DEST"
raw "$REF" "contract.json" > "${DEST}/contract.json"
raw "$REF" "CONTRACT.md"   > "${DEST}/CONTRACT.md"

VERSION="$(jq -r '.version' "${DEST}/contract.json")"

sha_of() { sha256sum "$1" | cut -d' ' -f1; }
JSON_SHA="$(sha_of "${DEST}/contract.json")"
MD_SHA="$(sha_of "${DEST}/CONTRACT.md")"

# Record the ref + per-file content digests (never the content itself) in the
# committed pin file, so the build path can verify integrity of every fetch.
tmp="$(mktemp)"
jq \
  --arg ref "$REF" \
  --arg version "$VERSION" \
  --arg json_sha "$JSON_SHA" \
  --arg md_sha "$MD_SHA" \
  '.ref = $ref
   | .version = $version
   | .sha256 = { "contract.json": $json_sha, "CONTRACT.md": $md_sha }' \
  "$PINNED" > "$tmp" && mv "$tmp" "$PINNED"

# Refresh the local ref stamp so hatch_build.py sees the cache as current.
printf '%s\n' "$REF" > "${DEST}/.ref"

echo "Pinned ref ${REF} (contract v${VERSION})."
echo "  contract.json sha256 ${JSON_SHA}"
echo "  CONTRACT.md   sha256 ${MD_SHA}"
echo "Content staged in contract/ (gitignored)."
echo "If you pinned a tag/branch, consider re-pinning the resolved commit SHA:"
echo "  git ls-remote ${REPO_URL:-https://github.com/${REPO}} ${REF}"
echo "Review the PINNED.json diff, then run: make conformance-ci"
