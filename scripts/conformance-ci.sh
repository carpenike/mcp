#!/usr/bin/env bash
#
# conformance-ci.sh — boot the server locally and assert conformance.
#
# Used by CI (and runnable locally). Boots homelab-mcp on a loopback port
# with dummy PocketID config (the discovery + DCR endpoints don't need a
# live IdP), waits for readiness, then runs:
#   1. the UPSTREAM conformance harness, cloned fresh at the pinned tag and
#      run UNPATCHED with --mcp-path /mcp  (pocketid-mcp-as v1.1, profile
#      jwt-refresh, scope mcp-only)
#   2. the upstream-aware served-contract drift guard (served == upstream@ref)
# Tears the server down and propagates a non-zero exit on any failure.
#
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PORT="${PORT:-9277}"
ORIGIN="http://127.0.0.1:${PORT}"
# Use `-` (not `:-`) so an explicitly-empty RUNNER stays empty. CI sets
# RUNNER="" to run the installed `homelab-mcp` console script directly (no uv);
# local dev leaves RUNNER unset and gets the `uv run` default.
RUNNER="${RUNNER-uv run}"
MCP_PATH="${MCP_PATH:-/mcp}"
REPO_URL="https://github.com/carpenike/mcp-as-contract"
REF="$(jq -r '.ref' "${ROOT}/contract/PINNED.json")"

for bin in curl jq git; do
  command -v "$bin" >/dev/null 2>&1 || { echo "missing required tool: $bin" >&2; exit 2; }
done

tmp="$(mktemp -d)"
export HOMELAB_MCP_PUBLIC_BASE_URL="$ORIGIN"
export HOMELAB_MCP_POCKETID_ISSUER="https://id.example.invalid"
export HOMELAB_MCP_POCKETID_CLIENT_ID="ci-dummy"
export HOMELAB_MCP_POCKETID_CLIENT_SECRET="ci-dummy"
export HOMELAB_MCP_OAUTH_SIGNING_KEY_PATH="${tmp}/signing-key.pem"
export HOMELAB_MCP_OAUTH_STATE_DB_PATH="${tmp}/state.db"
export HOMELAB_MCP_BIND_ADDRESS="127.0.0.1"
export HOMELAB_MCP_PORT="$PORT"

server_pid=""
cleanup() {
  [[ -n "$server_pid" ]] && kill "$server_pid" 2>/dev/null || true
  rm -rf "$tmp"
}
trap cleanup EXIT

# The hosting routes serve the contract from contract/ in a dev checkout; make
# sure it's staged (the wheel/editable build hook also does this, but a fresh
# checkout or a re-deleted gitignored file would otherwise be missing).
if [[ ! -f "${ROOT}/contract/contract.json" ]]; then
  echo "Staging contract content for the pinned ref ..."
  bash "${ROOT}/scripts/pull-contract.sh" "$REF" >/dev/null
fi

echo "Booting homelab-mcp on ${ORIGIN} ..."
# shellcheck disable=SC2086
( cd "$ROOT" && exec $RUNNER homelab-mcp ) &
server_pid=$!

ready=0
for _ in $(seq 1 60); do
  if curl -fsS -o /dev/null "${ORIGIN}/.well-known/oauth-authorization-server" 2>/dev/null; then
    ready=1; break
  fi
  if ! kill -0 "$server_pid" 2>/dev/null; then
    echo "server process exited before becoming ready" >&2; exit 1
  fi
  sleep 0.5
done
[[ "$ready" == "1" ]] || { echo "server did not become ready in time" >&2; exit 1; }
echo "Server ready."
echo

echo "=== Part A: upstream conformance harness (${REF}, unpatched) ==="
checker="${tmp}/mcp-as-contract"
# Fetch the exact pinned rev by ref. `git clone --branch` only accepts named
# refs (tags/branches), so it can't take the immutable commit SHA we now pin;
# init + fetch <ref> + checkout FETCH_HEAD works for a SHA, tag, or branch.
git init --quiet "$checker"
git -C "$checker" remote add origin "$REPO_URL"
git -C "$checker" fetch --quiet --depth 1 origin "$REF"
git -C "$checker" checkout --quiet FETCH_HEAD
bash "${checker}/conformance/check.sh" "$ORIGIN" jwt-refresh mcp-only --mcp-path "$MCP_PATH"
echo

echo "=== Part B: served-contract drift guard (served == upstream@${REF}) ==="
bash "${ROOT}/scripts/verify-served-contract.sh" "$ORIGIN" "$REF"

echo
echo "All conformance + drift checks passed."
