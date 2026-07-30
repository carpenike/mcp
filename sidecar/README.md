# Actual Budget sidecar

A ~250-line Node service that owns the `@actual-app/api` client so the Python
`finances_*` tools can query the household budget. It exists because Actual
has no HTTP query API and no API keys — the only supported programmatic client
is the Node package, which downloads the (end-to-end encrypted) budget file
locally and answers queries against it.

The split is deliberate: **this process holds the Actual client, the Python
side holds the arithmetic.** All the gap math, staleness rules and recurring
matching live in `src/homelab_mcp/tools/finances.py`, where they're unit
tested without a live budget.

## Why not a Python client?

`actualpy` was evaluated first and rejected. It decrypts the E2E budget file
correctly and caches its session token, but it cannot apply the budget's sync
messages: `download_budget()` raises

```
ActualError: Actual found a column not supported by the library:
column 'bank_sync_status' at table 'accounts' not found
```

That turned out **not** to be an actualpy limitation — the official
`@actual-app/api@26.6.0` fails identically with `no such column:
bank_sync_status`. See below.

## The two rules

### 1. Never let the client version exceed the sync server's

`@actual-app/api` applies its **bundled** migrations to the budget file. Run a
client newer than the server and it migrates the file to a schema the server
— and the server's web UI — cannot read.

This is not hypothetical. On 2026-07-30 the finances repo's `.mcp.json` ran
`npx -y actual-budget-mcp`, which resolves `@actual-app/api: "^26.6.0"` to the
newest 26.x. It pulled 26.7.0 against a 26.6.0 server, applied
`migrations/1780606215000_add_bank_sync_status.sql`, and wrote sync messages
referencing the new column. Every 26.6.0 client then failed to sync.

The version is pinned exactly in `package.json` and frozen by
`package-lock.json`. Before bumping it:

```bash
curl -s https://budget.holthome.net/info
```

and confirm the server is already at (or above) the target version. The
sidecar enforces this itself at boot (see below), but checking first saves a
failed deploy.

> **Current state (2026-07-30, resolved):** server, budget file and this pin
> are all at **26.7.0**. The sync server was moved to a pinned nixpkgs commit
> carrying 26.7.0, which closed the split that briefly locked the web UI out.

The check is enforced in code, not just documented. `boot()` fetches the
server's `/info` and **refuses to start** if this client is newer, *before*
`api.init()` — so the migration never happens and no login attempt is spent.
A client older than the server logs a warning and proceeds (safe for the file,
but the server may migrate it past what this client can read).

Both the server package and this pin are pinned, so they can't drift on their
own. What the interlock actually guards is the manual case: bumping one side
and forgetting the other. Bumping this pin ahead of the server is precisely
what caused the 2026-07-30 lockout, and pinned nixpkgs does not prevent that.

`/health` reports `client_version`, `server_version` and `version_ok` so a
mismatch alerts via gatus instead of surfacing as a weekly pulse with no data.

### 2. One login, ever

`POST /account/login` rate-limits at **5 failed attempts per 15 minutes**.
`api.init()` performs the single login for the life of the process and the
loaded budget is reused for every request. There is no retry loop anywhere in
`server.js`: a failed boot exits non-zero and lets systemd back off (the unit
sets `RestartSec=60s`, `StartLimitBurst=3`).

## Endpoints

Loopback bind plus a shared token (`X-Sidecar-Token`), because this process
holds a decrypted copy of the household's finances.

| Method | Path | Returns |
|---|---|---|
| `GET` | `/health` | `{ok, budget_loaded, last_sync_age_seconds, client_version, server_version, version_ok, error}` — unauthenticated, for gatus |
| `GET` | `/accounts` | id, name, offbudget, closed, `balance_cents` |
| `GET` | `/categories` | id, name, group, `is_income` |
| `GET` | `/transactions?start=&end=` | dated rows with account/category/payee, `is_transfer`, `amount_cents` |
| `POST` | `/bank-sync` | triggers Actual's SimpleFin pull, then re-syncs |

Amounts are Actual's integer **cents** on the wire; the Python side converts
to dollars once, at the tool boundary.

Reads serve the cached budget and re-sync it when older than
`SIDECAR_SYNC_TTL_SECONDS` (default 300). A failed re-sync is non-fatal —
stale-but-served beats a hard error, and `finances_sync_status` is the tool
whose job is to surface staleness.

## Configuration

| Env | Notes |
|---|---|
| `ACTUAL_SERVER_URL` | e.g. `https://budget.holthome.net` |
| `ACTUAL_PASSWORD` | server login password (secret) |
| `ACTUAL_BUDGET_SYNC_ID` | Settings → Advanced → Sync ID |
| `ACTUAL_ENCRYPTION_PASSWORD` | file encryption password; omit if not encrypted |
| `ACTUAL_DATA_DIR` | budget cache; created if absent (`api.init` does not create it) |
| `SIDECAR_HOST` / `SIDECAR_PORT` | default `127.0.0.1:9210` |
| `SIDECAR_TOKEN` | must equal `HOMELAB_MCP_FINANCES_SIDECAR_TOKEN` |
| `SIDECAR_SYNC_TTL_SECONDS` | default `300` |

Deployed via `services.homelab-mcp.actualSidecar` (see `nix/module.nix`).

## Local run

```bash
npm install
SIDECAR_TOKEN=dev ACTUAL_SERVER_URL=https://budget.holthome.net \
  ACTUAL_PASSWORD=... ACTUAL_BUDGET_SYNC_ID=... ACTUAL_ENCRYPTION_PASSWORD=... \
  ACTUAL_DATA_DIR=/tmp/actual-data node server.js
```
