#!/usr/bin/env bash
#
# verify-served-contract.sh — upstream-aware drift guard.
#
# Asserts the LIVE-served contract.json deep-equals the upstream contract.json
# at the pinned ref (served == upstream@pinned), NOT served == a committed
# copy. GitHub is the single source of truth, so this catches both an
# accidental serving bug AND a stale pin shipping content that no longer
# matches what we claim to serve. Also checks the public CORS posture, the
# X-Contract-Version header (must equal the upstream version), the content
# type, and that /contract is reachable.
#
# Usage:
#   scripts/verify-served-contract.sh <origin> [ref]
#   e.g. scripts/verify-served-contract.sh https://mcp.holthome.net
#        scripts/verify-served-contract.sh http://127.0.0.1:9200 v1.1.0
#
set -euo pipefail

ORIGIN="${1:-}"
[[ -n "$ORIGIN" ]] || { echo "usage: $0 <origin> [ref]" >&2; exit 2; }
ORIGIN="${ORIGIN%/}"

REPO="carpenike/mcp-as-contract"
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PINNED="${ROOT}/contract/PINNED.json"

for bin in curl jq; do
  command -v "$bin" >/dev/null 2>&1 || { echo "missing required tool: $bin" >&2; exit 2; }
done
[[ -f "$PINNED" ]] || { echo "contract/PINNED.json not found at $PINNED" >&2; exit 2; }

REF="${2:-$(jq -r '.ref' "$PINNED")}"
[[ -n "$REF" && "$REF" != "null" ]] || { echo "no pinned ref" >&2; exit 1; }

upstream="$(mktemp)"
served="$(mktemp)"
trap 'rm -f "$upstream" "$served"' EXIT

echo "Upstream pin: ${REPO}@${REF}"
curl -fsS "https://raw.githubusercontent.com/${REPO}/${REF}/contract.json" -o "$upstream"
upstream_version="$(jq -r '.version' "$upstream")"
echo "Upstream contract version: ${upstream_version}"

fail=0
JSON_URL="${ORIGIN}/.well-known/mcp-as-contract.json"
echo "Fetching ${JSON_URL}"
headers="$(curl -fsS -D - -o "$served" "$JSON_URL")"

# 1) Deep-equal: served JSON == upstream@ref JSON (order-insensitive).
if jq -e --slurpfile a "$served" --slurpfile b "$upstream" -n '$a[0] == $b[0]' >/dev/null; then
  echo "  PASS  served contract.json deep-equals upstream@${REF}"
else
  echo "  FAIL  served contract.json DIFFERS from upstream@${REF}"
  diff <(jq -S . "$upstream") <(jq -S . "$served") || true
  fail=1
fi

# 2) CORS open to all origins.
if grep -iq '^access-control-allow-origin: \*' <<<"$headers"; then
  echo "  PASS  Access-Control-Allow-Origin: *"
else
  echo "  FAIL  missing Access-Control-Allow-Origin: *"; fail=1
fi

# 3) X-Contract-Version header matches the upstream version.
served_version="$(grep -i '^x-contract-version:' <<<"$headers" | tr -d '\r' | awk '{print $2}')"
if [[ "$served_version" == "$upstream_version" ]]; then
  echo "  PASS  X-Contract-Version: ${served_version}"
else
  echo "  FAIL  X-Contract-Version '${served_version}' != upstream '${upstream_version}'"; fail=1
fi

# 4) application/json content type.
if grep -iq '^content-type: application/json' <<<"$headers"; then
  echo "  PASS  Content-Type: application/json"
else
  echo "  FAIL  Content-Type is not application/json"; fail=1
fi

# 5) The human CONTRACT.md is reachable.
md_status="$(curl -fsS -o /dev/null -w '%{http_code}' "${ORIGIN}/contract" || true)"
if [[ "$md_status" == "200" ]]; then
  echo "  PASS  GET /contract -> 200"
else
  echo "  FAIL  GET /contract -> ${md_status}"; fail=1
fi

[[ "$fail" -eq 0 ]] && echo "OK: served contract matches upstream@${REF}" || echo "DRIFT DETECTED"
exit "$fail"
