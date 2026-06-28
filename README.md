# homelab-mcp

A small MCP server that exposes homelab APIs (cooklang recipes, gatus uptime monitoring, grocy household management) as
tools that Claude can call. Deployed on forge. Runs its own OAuth 2.1 Authorization Server
that federates user logins to PocketID.

**Status:** v0.2 — embedded OAuth provider (replaces v0.1's Cloudflare Access dependency).

## What this is

An [MCP](https://modelcontextprotocol.io) server speaking the
[Streamable HTTP transport](https://modelcontextprotocol.io/specification/2025-03-26/basic/transports).
It runs on the homelab, exposes a handful of tools wrapping internal APIs, mints its own
RS256 JWTs via an embedded OAuth 2.1 Authorization Server, and validates every request
against those JWTs before dispatching.

**One server, many tool categories.** Each category lives in its own module under
`src/homelab_mcp/tools/`. Adding a new category is dropping a file there; the registry
auto-discovers it. No central wiring file to update.

## Why a custom OAuth provider?

The MCP custom-connector spec (RFC 9728 + RFC 8414 + RFC 7591) requires the resource server
to advertise an authorization server that supports:

  - **Dynamic Client Registration (DCR)** — Claude registers itself without operator action.
  - **PKCE-protected authorization-code grant** — standard OAuth 2.1.
  - **Refresh tokens** — the token endpoint also supports the `refresh_token` grant and hands
    out a (rotating) refresh token with every access token, so clients renew expired access
    tokens silently instead of re-running the interactive login. Access tokens default to a
    24h lifetime (`HOMELAB_MCP_OAUTH_ACCESS_TOKEN_LIFETIME_SECONDS`); refresh tokens default
    to 30 days (`HOMELAB_MCP_OAUTH_REFRESH_TOKEN_LIFETIME_SECONDS`).
  - **Restart-survivable sessions** — registered clients and refresh tokens are persisted to a
    small SQLite store (`HOMELAB_MCP_OAUTH_STATE_DB_PATH`, default `/var/lib/homelab-mcp/state.db`),
    so a service restart or redeploy doesn't force a re-login. Refresh tokens are stored as
    SHA-256 hashes (never plaintext) and are revocable by deleting their row. Set the path to
    `:memory:` to opt out (clients re-register and users re-authenticate on every restart).
  - **Bounded DCR growth** — abandoned clients (no live refresh token, older than
    `HOMELAB_MCP_OAUTH_CLIENT_RETENTION_SECONDS`, default 90d) are pruned at startup and on each
    registration, and the unauthenticated `/oauth/register` endpoint is rate-limited per source IP
    (`HOMELAB_MCP_OAUTH_REGISTER_RATE_LIMIT_MAX` per `…_RATE_WINDOW_SECONDS`, default 30/hour) so
    the persisted client table can't grow without bound.
  - **Spec-compliant metadata** — field names like `grant_types_supported = ["authorization_code"]`,
    not Cloudflare's `["authorization_code_with_pkce"]`.

Neither of the obvious off-the-shelf options work:

  - **PocketID** doesn't implement DCR.
  - **Cloudflare Access for SaaS (OIDC)** returns non-standard field names in its discovery
    doc, which Claude silently rejects.

So we run our own spec-clean OAuth AS in-process and federate the actual user login (passkey)
upstream to PocketID. Claude never touches PocketID directly.

## Contract conformance

This server **conforms to [pocketid-mcp-as](https://github.com/carpenike/mcp-as-contract)
v1.1, profile `jwt-refresh`, scope `mcp-only`, MCP path `/mcp`.**

`pocketid-mcp-as` is the shared contract for the self-hosted MCP OAuth 2.1
Authorization Servers that federate login to PocketID across several
carpenike apps (`replog`, `whiskey-whiskey-whiskey`, `marginalia`, and this
one). It standardizes the discovery field names, OAuth wire behavior, and
discovery documents — not the token storage model, and (since v1.1) not the
MCP resource path, which is app-declared. This app uses the `jwt-refresh`
profile (RS256 access tokens + rotating refresh tokens, publishes
`jwks_uri`), the `mcp-only` scope posture (the minted token is accepted only
on the `/mcp` resource path), and keeps its original `/mcp` transport path.
The path-suffixed RFC 9728 §3.3 PRM, its `resource`, and the §1.7
`WWW-Authenticate` hint are all derived from the single `mcp_path` setting.

Run the upstream conformance harness against a live AS. It's cloned fresh at
the pinned tag (`contract/PINNED.json`) and run **unpatched** with the path
flag — the v1.1.0 harness fixed the earlier subshell bug, so we no longer
vendor or patch it:

```bash
make conformance ORIGIN=http://127.0.0.1:9200     # local dev
make conformance ORIGIN=https://mcp.holthome.net  # production
# which clones the pinned tag and runs:
#   conformance/check.sh <origin> jwt-refresh mcp-only --mcp-path /mcp
```

CI boots the server and runs both the upstream harness and the drift guard
on every push (`make conformance-ci`).

### Hosting the contract (mcp.holthome.net is its public home)

This host serves the canonical public copy of the contract so other repos'
build/CI harnesses can fetch the spec at runtime. Both routes are
unauthenticated, GET-only, CORS-open (`Access-Control-Allow-Origin: *`), and
live entirely outside the OAuth/bearer path:

| Route | Serves | Headers |
|-------|--------|---------|
| `/.well-known/mcp-as-contract.json` | machine-readable `contract.json` | `application/json`, `Cache-Control: public, max-age=300`, `X-Contract-Version` |
| `/contract` | human-readable `CONTRACT.md` (raw) | `text/markdown`, same cache + version headers |

**GitHub is the single source of truth — we don't commit the contract into
this tree.** The content is fetched at wheel-build time
([`hatch_build.py`](hatch_build.py)) from the ref pinned in
[`contract/PINNED.json`](contract/PINNED.json) and force-included into the
wheel as package data, so the running server is self-contained (no runtime
GitHub dependency) but the source carries no copy (the fetched files under
`contract/` are gitignored). Bumping the pin is a deliberate, reviewable
step:

```bash
make contract-pull REF=v1.1.0   # update the pinned ref; review PINNED.json diff
```

The drift guard is **upstream-aware**: CI fetches `contract.json` from the
pinned tag on GitHub and asserts the live-served bytes deep-equal it
(served == upstream@pinned), so a serving bug *or* a stale pin is caught.

## Tools

| Category | Tool name | What it does |
|----------|-----------|--------------|
| Cooklang | `cooklang_list_recipes` | List your canonical cookbook (cook.holthome.net) with optional course/cuisine/tag/text filters |
| Cooklang | `cooklang_get_recipe` | Fetch one recipe's metadata + ingredients/cookware/steps, by slug or path |
| Cooklang | `cooklang_search_recipes` | Search YOUR recipes by name/metadata (opt-in ingredient matching) |
| Cooklang | `cooklang_create_recipe` | Author a NEW `.cook` (frontmatter + body); `derived_from` is first-class; fails on collision |
| Cooklang | `cooklang_update_recipe` | Amend an existing recipe (parser-validated before overwrite); can also move/rename it via `new_folder`/`new_slug` |
| Cooklang | `cooklang_delete_recipe` | Permanently delete a recipe; previews the target unless `confirm=true` |
| Cooklang | `cooklang_search_federation` | Search the federated index (your repo + ~60 community feeds) |
| Cooklang | `cooklang_build_shopping_list` | Combine ingredients across multiple of YOUR recipes, grouped by store aisle |
| Homelab | `homelab_list_status` | Snapshot of all monitored endpoints via gatus |
| Homelab | `homelab_get_endpoint_history` | Recent check history for one specific endpoint |
| Grocy | `grocy_stock_item` | Keystone walkthrough tool: find-or-create a product then set/add/consume in one call; price + store on `add` |
| Grocy | `grocy_find_products` | Find products by name across ALL master data ("do we have X?") |
| Grocy | `grocy_list_stock` | Everything currently in stock with amount + next due date |
| Grocy | `grocy_expiring` | Planning feed: due soon / overdue / expired / missing |
| Grocy | `grocy_consume_product` | Consume/spoil primitive (by id or barcode) |
| Grocy | `grocy_open_product` | Mark an amount of a product as opened (by id or barcode) |
| Grocy | `grocy_ensure_location` | Idempotently create a storage location |
| Grocy | `grocy_ensure_unit` | Idempotently create a quantity unit |
| Grocy | `grocy_ensure_store` | Idempotently create/update a store (shopping location); optional `address` in a dedicated userfield |
| Grocy | `grocy_seed_defaults` | One-shot bootstrap of default locations + units (idempotent) |
| Grocy | `grocy_health` | Connectivity + Grocy version check |
| Grocy | `grocy_convert_units` | Convert an amount between units (product-specific → global → identity) |
| Grocy | `grocy_product_card` | Enriched product detail: on-hand, min/below-min, price, shelf life, locations |
| Grocy | `grocy_consumption_history` | Burn rate from the stock log (purchased/consumed/spoiled + rates) |
| Grocy | `grocy_stock_value` | Total inventory value, optionally by location + top-N products |
| Grocy | `grocy_restock_suggestions` | Quantity-driven below-minimum signal (vs. date-driven `grocy_expiring`) |
| Grocy | `grocy_stock_by_location` | On-hand stock grouped by storage location |
| Grocy | `grocy_set_unit_conversion` | Upsert a unit conversion (product-specific or global); write one direction |
| Grocy | `grocy_list_unit_conversions` | Inspect defined conversions (global and/or per product) |

## Architecture

```
┌─────────────────────┐
│  Claude (mobile)    │
└──────────┬──────────┘
           │ 1. DCR + 2. /authorize
           ▼
┌──────────────────────────────────────────────────────────┐
│  homelab-mcp  (mcp.holthome.net, via Cloudflare Tunnel)  │
│                                                          │
│   ├─ /.well-known/oauth-protected-resource (RFC 9728)    │
│   ├─ /.well-known/oauth-protected-resource/mcp (RFC 9728 §3.3, VS Code) │
│   ├─ /.well-known/oauth-authorization-server (RFC 8414)  │
│   ├─ /.well-known/mcp-as-contract.json (hosted contract, public) │
│   ├─ /contract              (hosted CONTRACT.md, public)  │
│   ├─ /oauth/jwks.json     (public verifier key)          │
│   ├─ /oauth/register      (RFC 7591 DCR)                 │
│   ├─ /oauth/authorize ────► 302 to PocketID              │
│   ├─ /oauth/callback ◄──── PocketID returns code         │
│   ├─ /oauth/token         (PKCE-verified, mints RS256)   │
│   └─ /mcp                 (FastMCP transport + JWT)      │
└──────────┬──────────┬────────────────────────────────────┘
           │          │
           │          └─► PocketID (id.holthome.net) — passkey login
           │
           ├──► fedcook.holthome.net  (federation search)
           ├──► cook.holthome.net     (CookLang recipes: read + author + shopping list)
           ├──► gatus.holthome.net    (uptime monitoring)
           └──► grocy.holthome.net    (food inventory: stock + master data)
```

JWTs are RS256, signed by a 2048-bit RSA key resident on the host. The key comes from one of:

  1. `HOMELAB_MCP_OAUTH_SIGNING_KEY` env var (sops-managed; preferred — key never touches disk)
  2. `HOMELAB_MCP_OAUTH_SIGNING_KEY_PATH` file (sops-mounted secret)
  3. auto-generated and persisted to `/var/lib/homelab-mcp/signing-key.pem` (0600) on first run

The matching public key is published at `/oauth/jwks.json` so any external verifier (or our
own middleware) can validate offline.

## Local development

Requires Nix with flakes and direnv:

```bash
cd ~/src/mcp
direnv allow
# devshell loads python313 + all deps + ruff + mypy + pytest
```

Or manually:

```bash
nix develop
```

Run the server locally (OAuth disabled — local loopback only):

```bash
HOMELAB_MCP_OAUTH_REQUIRED=false \
HOMELAB_MCP_BIND_ADDRESS=127.0.0.1 \
HOMELAB_MCP_PORT=9200 \
homelab-mcp
```

Probe it:

```bash
curl -s http://127.0.0.1:9200/mcp -H 'Content-Type: application/json' \
  -d '{"jsonrpc":"2.0","id":1,"method":"tools/list"}'
```

## Tests

```bash
pytest -v
```

Coverage focuses on the security-critical bits:
  - `tests/test_auth.py` — JWT validation rejection paths (real RSA keypair).
  - `tests/test_oauth_flow.py` — end-to-end OAuth dance with PocketID mocked.
  - `tests/test_tools_cooklang.py` — recipe CRUD against a mocked CookLang wire + slug/path-traversal hardening.
  - `tests/test_app.py` — discovery + allowlist + middleware wiring.

## Deployment

This repo ships a NixOS module at `flake.nixosModules.default`. Consumer pattern (in
`carpenike/nix-config`):

```nix
# flake.nix
inputs.homelab-mcp = {
  url = "github:carpenike/mcp";
  inputs.nixpkgs.follows = "nixpkgs";
};

# hosts/forge/services/homelab-mcp.nix
{ config, inputs, pkgs, ... }: {
  imports = [ inputs.homelab-mcp.nixosModules.default ];

  services.homelab-mcp = {
    enable = true;
    package = inputs.homelab-mcp.packages.${pkgs.system}.default;

    publicBaseUrl = "https://mcp.holthome.net";

    settings = {
      HOMELAB_MCP_POCKETID_ISSUER     = "https://id.holthome.net";
      HOMELAB_MCP_POCKETID_CLIENT_ID  = "<from PocketID admin UI>";
      HOMELAB_MCP_COOKLANG_BASE_URL   = "https://cook.holthome.net";
      HOMELAB_MCP_FEDERATION_BASE_URL = "https://fedcook.holthome.net";
      HOMELAB_MCP_GATUS_BASE_URL      = "https://gatus.holthome.net";
    };

    # sops-managed env file with at minimum:
    #   HOMELAB_MCP_POCKETID_CLIENT_SECRET=...
    # Optionally:
    #   HOMELAB_MCP_OAUTH_SIGNING_KEY=<RSA PEM, PKCS#8, escaped \n>
    #   HOMELAB_MCP_OAUTH_SESSION_SECRET=<32+ random bytes>
    environmentFile = config.sops.secrets."homelab-mcp/env".path;
  };
}
```

### PocketID client setup (one-time)

In PocketID admin UI, create an OIDC client with:

  - **Callback URL:** `https://mcp.holthome.net/oauth/callback`
  - **Scopes:** `openid email profile`

Copy the client ID into `HOMELAB_MCP_POCKETID_CLIENT_ID` and the client secret into the
sops env file as `HOMELAB_MCP_POCKETID_CLIENT_SECRET`.

See [`AGENTS.md`](AGENTS.md) for the conventions an AI coding agent (or human) should follow
when extending this.

## License

MIT
