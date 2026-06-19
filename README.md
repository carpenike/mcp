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
  - **Spec-compliant metadata** — field names like `grant_types_supported = ["authorization_code"]`,
    not Cloudflare's `["authorization_code_with_pkce"]`.

Neither of the obvious off-the-shelf options work:

  - **PocketID** doesn't implement DCR.
  - **Cloudflare Access for SaaS (OIDC)** returns non-standard field names in its discovery
    doc, which Claude silently rejects.

So we run our own spec-clean OAuth AS in-process and federate the actual user login (passkey)
upstream to PocketID. Claude never touches PocketID directly.

## Tools

| Category | Tool name | What it does |
|----------|-----------|--------------|
| Cooklang | `cooklang_list_recipes` | List your canonical cookbook (cook.holthome.net) with optional course/cuisine/tag/text filters |
| Cooklang | `cooklang_get_recipe` | Fetch one recipe's metadata + ingredients/cookware/steps, by slug or path |
| Cooklang | `cooklang_search_recipes` | Search YOUR recipes by name/metadata (opt-in ingredient matching) |
| Cooklang | `cooklang_create_recipe` | Author a NEW `.cook` (frontmatter + body); `derived_from` is first-class; fails on collision |
| Cooklang | `cooklang_update_recipe` | Amend an existing recipe; parser-validated before it overwrites |
| Cooklang | `cooklang_search_federation` | Search the federated index (your repo + ~60 community feeds) |
| Cooklang | `cooklang_build_shopping_list` | Combine ingredients across multiple of YOUR recipes, grouped by store aisle |
| Homelab | `homelab_list_status` | Snapshot of all monitored endpoints via gatus |
| Homelab | `homelab_get_endpoint_history` | Recent check history for one specific endpoint |
| Grocy | `grocy_list_stock` | Products currently in stock with amount + next due date |
| Grocy | `grocy_list_volatile_stock` | What needs attention: due soon / overdue / expired / missing |
| Grocy | `grocy_get_product` | Full stock detail for one product (by id or barcode) |
| Grocy | `grocy_add_product` | Add/purchase stock for a product (by id or barcode) |
| Grocy | `grocy_consume_product` | Consume or spoil stock for a product (by id or barcode) |
| Grocy | `grocy_open_product` | Mark an amount of a product as opened |
| Grocy | `grocy_list_shopping_list` | List shopping-list items (optionally filtered to one list) |
| Grocy | `grocy_add_shopping_list_product` | Add a product to a shopping list |
| Grocy | `grocy_remove_shopping_list_product` | Remove an amount of a product from a shopping list |
| Grocy | `grocy_add_missing_products` | Bulk-add products below min stock to a shopping list |
| Grocy | `grocy_add_overdue_products` | Bulk-add overdue products to a shopping list |
| Grocy | `grocy_list_chores` | All chores with next estimated execution time |
| Grocy | `grocy_get_chore` | Details/history for one chore |
| Grocy | `grocy_track_chore` | Track (or skip) a chore execution |
| Grocy | `grocy_list_tasks` | Open tasks with due dates |
| Grocy | `grocy_complete_task` | Mark a task completed |

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
           └──► grocy.holthome.net    (stock, shopping list, chores, tasks)
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
