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
#     ref   tag/branch/SHA to pin (default: keep the current pin, just refetch)
#
# Bumping the pin is a deliberate, reviewable step: run with a new tag, then
# review the PINNED.json diff and re-run `make conformance-ci`.
#
set -euo pipefail

REPO="carpenike/mcp-as-contract"
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
DEST="${ROOT}/contract"
PINNED="${DEST}/PINNED.json"

for bin in curl jq; do
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

# Record only the ref (never the content) in the committed pin file.
tmp="$(mktemp)"
jq --arg ref "$REF" '.ref = $ref' "$PINNED" > "$tmp" && mv "$tmp" "$PINNED"

echo "Pinned ref ${REF} (contract v${VERSION}). Content staged in contract/ (gitignored)."
echo "Review the PINNED.json diff, then run: make conformance-ci"
