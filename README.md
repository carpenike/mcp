# homelab-mcp

A small MCP server that exposes homelab APIs (cooklang recipes, gatus uptime monitoring) as
tools that Claude can call. Deployed on forge behind Cloudflare Access for authentication.

**Status:** v0.1 — first working release.

## What this is

An [MCP](https://modelcontextprotocol.io) server speaking the [Streamable HTTP transport](https://modelcontextprotocol.io/specification/2025-03-26/basic/transports). It runs on the homelab, exposes a handful of tools wrapping internal APIs, and validates every request against a Cloudflare Access JWT before dispatching.

**One server, many tool categories.** Each category lives in its own module under `src/homelab_mcp/tools/`. Adding a new category is dropping a file there; the registry auto-discovers it. No central wiring file to update.

## Tools

| Category | Tool name | What it does |
|----------|-----------|--------------|
| Cooklang | `cooklang_search_recipes` | Search the federated recipe index (your repo + 61 community feeds) |
| Cooklang | `cooklang_get_recipe` | Fetch a recipe's structured details (federation or your personal cookbook) |
| Cooklang | `cooklang_list_my_recipes` | List recipes in your personal cookbook |
| Cooklang | `cooklang_build_shopping_list` | Combine ingredients across multiple of YOUR recipes, grouped by store aisle |
| Cooklang | `cooklang_save_recipe` | Save a new `.cook` file to your personal cookbook (Resilio + git auto-sync carry it everywhere) |
| Homelab | `homelab_list_status` | Snapshot of all monitored endpoints via gatus |
| Homelab | `homelab_get_endpoint_history` | Recent check history for one specific endpoint |

## Architecture

```
┌─────────────────────┐
│  Claude.ai          │  bearer = CF Access JWT
│  (web / mobile)     │
└──────────┬──────────┘
           │ Streamable HTTP
           ▼
┌─────────────────────┐
│  Cloudflare Access  │  Access for SaaS (OIDC), federated to PocketID
│  (mcp.holthome.net) │
└──────────┬──────────┘
           │ Tunnel
           ▼
┌─────────────────────┐
│  Caddy on forge     │
└──────────┬──────────┘
           │ 127.0.0.1:9200
           ▼
┌─────────────────────┐    ┌────────────────────────────┐
│  homelab-mcp.svc    ├───►│  JWTAuthMiddleware         │
│  (this project)     │    │  validates against         │
│                     │    │  team.cloudflareaccess.com │
│                     │    │  JWKS                       │
└──────────┬──────────┘    └────────────────────────────┘
           │
           ├──► fedcook.holthome.net  (federation search)
           ├──► cook.holthome.net     (CookCLI recipes/shopping list)
           ├──► /data/cooklang/recipes/claude/  (save_recipe writes here)
           └──► gatus.holthome.net    (uptime monitoring)
```

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

Run the server locally (auth disabled for dev):

```bash
HOMELAB_MCP_CF_ACCESS_REQUIRED=false \
HOMELAB_MCP_BIND_ADDRESS=127.0.0.1 \
HOMELAB_MCP_PORT=9200 \
homelab-mcp
```

Probe it:

```bash
# health (no auth required, just hits the MCP transport)
curl -s http://127.0.0.1:9200/mcp -H 'Content-Type: application/json' -d '{"jsonrpc":"2.0","id":1,"method":"tools/list"}'
```

## Tests

```bash
pytest -v
```

Most tests are for the auth middleware (the security-critical bit) and the save-recipe path-traversal hardening. Tool handlers themselves are mostly thin wrappers and are exercised via integration testing against a live forge.

## Deployment

This repo ships a NixOS module at `flake.nixosModules.default`. Consumer pattern (in `carpenike/nix-config`):

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

    settings = {
      HOMELAB_MCP_CF_ACCESS_TEAM = "bigheadltd";
      HOMELAB_MCP_COOKLANG_BASE_URL = "https://cook.holthome.net";
      HOMELAB_MCP_FEDERATION_BASE_URL = "https://fedcook.holthome.net";
      HOMELAB_MCP_GATUS_BASE_URL = "https://gatus.holthome.net";
    };

    environmentFile = config.sops.secrets."homelab-mcp/env".path;
    # contains: HOMELAB_MCP_CF_ACCESS_APP_ID=<OIDC Client ID from CF Access SaaS app>
  };
}
```

See [`AGENTS.md`](AGENTS.md) for the conventions an AI coding agent (or human) should follow when extending this.

## License

MIT
