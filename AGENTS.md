# homelab-mcp — Agent Instructions

> You're working on a small Model Context Protocol server that bridges
> Claude (over an embedded OAuth 2.1 provider) into homelab APIs. Single
> user, single deployment target (forge), conservative scope. **One server,
> multiple tool categories namespaced by prefix.**

## Quick orientation

- **What it is:** A Python Streamable-HTTP MCP server. Runs its own OAuth
  2.1 Authorization Server (RFC 8414 + 7591 + 9728 + PKCE) that federates
  the user login upstream to PocketID. Validates the RS256 JWTs *we mint*
  on every MCP request.
- **Stack:** Python 3.12+ · `mcp` SDK · Starlette/uvicorn · httpx · authlib
  (JOSE + OAuth primitives) · pyjwt (verifier path) · cryptography.
- **Auth (new in v0.2):** WE are the Authorization Server Claude talks to.
  PocketID (`id.holthome.net`) is the upstream OIDC IdP we federate to for
  passkey login. There is **no Cloudflare Access for SaaS dependency** —
  it was removed because its OIDC discovery doc uses non-spec field names
  Claude rejects.
  - RSA-2048 signing key resident on the host (sops-managed PEM or
    auto-generated/persisted to `/var/lib/homelab-mcp/signing-key.pem`).
  - Public key served at `/oauth/jwks.json`.
  - `JWTAuthMiddleware` verifies in-process against the public key — no
    network calls per request.
- **Deployment:** NixOS module in `nix/module.nix`, consumed by
  `carpenike/nix-config` as a flake input. Service runs as the
  `homelab-mcp` system user (extra group: `cooklang`, so it can write to
  `/data/cooklang/recipes/claude/`).

## Contract conformance (pocketid-mcp-as)

This server **conforms to [pocketid-mcp-as](https://github.com/carpenike/mcp-as-contract)
v1.2, profile `jwt-refresh`, scope `mcp-only`, MCP path `/mcp`.** That contract
is the single source of truth that keeps this AS aligned with the sibling apps
(`replog`, `whiskey-whiskey-whiskey`, `marginalia`). It standardizes the
discovery field names + OAuth wire behavior, NOT the token storage model, and
(since v1.1) NOT the MCP resource path, which is app-declared. v1.2 added the
redirect-URI hardening rule (parsed match + userinfo rejection) this app
reported; see security non-negotiable #6.

- **MCP resource path is `/mcp`** (app-declared, v1.1). It comes from
  `settings.mcp_path` and is wired into FastMCP's `streamable_http_path`; the
  RFC 9728 §3.3 path-suffixed PRM, its `resource` byte-match, and the §1.7
  `WWW-Authenticate` hint all derive from that one setting so the transport
  URL and advertised resource can never drift. Don't move it to `/api/mcp`.
- **Discovery field names are load-bearing.** A non-spec field name silently
  breaks Claude with no client-side log. If you touch the RFC 8414 metadata
  (`oauth_provider.authorization_server_metadata`) or the RFC 9728 PRM docs
  (`app._build_protected_resource_metadata`), run `make conformance-ci`.
- **Profile `jwt-refresh` is intentional** — do NOT convert this app to opaque
  tokens to match the others. AS metadata MUST publish `jwks_uri` and the
  token endpoint MUST support `grant_type=refresh_token`.
- **Conformance harness is NOT vendored.** `make conformance` / `conformance-ci`
  clone the harness fresh at the pinned ref (`contract/PINNED.json`) and run it
  **unpatched** with `--mcp-path /mcp`. The v1.2.0 harness adds the redirect-URI
  userinfo-bypass probe and the §1.7 challenge check; there's no patch to maintain.
- **mcp.holthome.net hosts the contract publicly** (see `homelab_mcp.contract`):
  `/.well-known/mcp-as-contract.json` + `/contract`, unauthenticated, GET-only,
  CORS-open, outside the bearer path. **GitHub is the single source of truth —
  the contract content is NOT committed.** `hatch_build.py` fetches it at
  build time from the pinned ref into the wheel; `contract/contract.json` +
  `CONTRACT.md` are gitignored. Bump the pin with `make contract-pull REF=<tag>`.
  The drift guard is upstream-aware: CI asserts served == upstream@pinned.
- Run `make conformance` / `make conformance-ci` after any change to the AS,
  its discovery, or the contract-hosting endpoints.

## Tool registry pattern (the architectural decision)

Each file under `src/homelab_mcp/tools/` that exports a top-level
`register(mcp, settings)` function gets auto-discovered at startup.

```python
# src/homelab_mcp/tools/photos.py  (hypothetical new category)
def register(mcp, settings):
    @mcp.tool(
        name="photos_search",  # namespaced: <category>_<verb>_<object>
        description="Search the immich photo library by free-text query.",
    )
    async def search(query: str, limit: int = 10) -> dict:
        ...
```

**Naming convention:**

- Tool `name`: `<category>_<verb>_<object>` (snake_case, no dots/hyphens/colons — safest across all MCP clients)
- Tool `description`: doubles as the human-facing label Claude shows the user; write it as if a human were reading it

**Rules:**

1. Files starting with `_` are ignored (e.g. `_registry.py`, `_http.py`).
   The registry isolates each module: an import error or a raising
   `register()` in one category is logged and skipped, not fatal to the rest.
2. Each module gets its own `register(mcp, settings)` function — keep
   tool definitions inside that closure so they can capture URLs from
   `settings`. Create ONE long-lived HTTP client per module with
   `tools._http.make_client(...)` and reuse it — don't build an
   `httpx.AsyncClient` per call.
3. Always validate inputs at the tool boundary. Path-like inputs need
   strict regexes (see `cooklang.NAME_RE`) and single-segment encoding
   (`tools._http.enc`) so a value can't rewrite the request path.
4. Never raise to the MCP transport. Use the shared error contract in
   `tools._http`: raise `ToolError(code, message, hint)` (or let
   `request_json` map upstream/transport/non-JSON failures to it) and
   return `err.payload()` at the boundary. Every tool's error is the same
   shape — `{"error": {"code": ..., "message": ..., "hint": ...}}` — so
   Claude learns it once. `request_json` also guards `resp.json()` (an
   SSO/proxy 200-HTML page must not throw a decode error to the transport).
5. Tool descriptions matter — they're what Claude uses to decide which
   tool to call. Be specific about WHEN to use the tool, not just what
   it does.
6. List-shaped outputs report truncation explicitly:
   `{"returned": n, "total": m, "truncated": bool}`. Never silently drop
   rows — a confident partial answer is worse than a flagged one.

## Adding a new tool category (the happy path)

1. Pick a category prefix (e.g. `photos`, `nas`, `lights`).
2. Create `src/homelab_mcp/tools/<category>.py`.
3. Add any new settings to `src/homelab_mcp/config.py` (e.g.
   `photos_base_url: str = "..."`).
4. Add the URL to `services.homelab-mcp.settings` in the host file
   in `carpenike/nix-config`.
5. Write the `register(mcp, settings)` function with one or more
   `@mcp.tool(...)` definitions.
6. Add a smoke test under `tests/test_tools_<category>.py`.
7. Update `README.md`'s tool table.
8. PR.

No edits to `app.py` or `_registry.py` are needed. That's the whole
point of the pattern.

## Security non-negotiables

These are the things that have to stay right, no matter what feature
gets added next:

1. **JWT validation happens BEFORE any tool dispatch.** The
   `JWTAuthMiddleware` is added to the Starlette app in `app.py`; any
   path past it has been auth'd.
2. **No tool writes recipes outside the CookLang server's recipe tree.**
   The recipe writers (`cooklang_create_recipe`, `cooklang_update_recipe`,
   `cooklang_delete_recipe`)
   go through cook.holthome.net's `PUT`/`DELETE /api/recipes/<relpath>`
   API — they
   never touch the filesystem directly. The systemd sandbox reflects this:
   the service is granted NO write access to the recipe tree (only its
   `StateDirectory`), so even a compromised process can't corrupt recipes
   directly, bypassing the parser validation the HTTP API enforces. Slugs
   are constrained by `NAME_RE = [a-zA-Z0-9_-]+`; folder/path segments are
   sanitised by `_sanitize_relpath` (no `..`, no `\`, no NUL, no absolute
   paths) before they reach the wire. New authored recipes default to the
   `claude/` subfolder. Every write is round-trip validated through the
   CookLang parser on a throwaway path BEFORE the real target is written,
   and `create` refuses to clobber an existing recipe unless
   `overwrite=True`. `delete` is destructive, so it refuses to act unless
   `confirm=True` — without it, it returns a non-destructive preview of the
   resolved target. Authored frontmatter keys are validated
   (`^[A-Za-z0-9_-]+$`) so metadata can't inject arbitrary YAML lines.
3. **No tool shells out without explicit input sanitisation.** Today no
   tool shells out at all. If a future tool does, it MUST use `subprocess`
   with `shell=False` and a fully validated argv list.
4. **Upstream URLs come from `Settings`, never from user input.** A tool
   that took a URL parameter and `httpx.get`'d it would be an SSRF vector.
5. **Tokens never reach logs.** `auth.py` logs `kid` and `iss` but never
   the token contents. Keep it that way.
6. **Redirect-URI matching parses the URL — never a bare `startswith`.**
   `oauth_provider._redirect_allowed` rejects any redirect target carrying
   userinfo (`user:pass@host`) or a malformed scheme/host BEFORE the prefix
   check. A naive `startswith` on a `:`-terminated loopback prefix is
   bypassable (`http://localhost:1@evil.com/`). Don't revert it. This was
   reported upstream and adopted into the `pocketid-mcp-as` contract in
   v1.2.0 (`dcr.match = "parsed-scheme-host-port"` + `dcr.reject_userinfo`,
   with a conformance probe), which this implementation passes.
7. **Refresh tokens rotate within a family with reuse detection.** Replaying
   an already-rotated refresh token revokes the whole rotation family
   (`oauth_state.consume_refresh`). Keep `family_id` threaded through both
   grants in `oauth_provider`.
8. **Home Assistant is a physical control plane — its tools are gated,
   closed-loop, and audited.** `ha_call_service` refuses any domain not on
   the operator-configured allowlist (`HOMELAB_MCP_HA_DOMAIN_ALLOWLIST`),
   checked for BOTH the service domain and the target entity's domain, so
   an allowlisted service can't smuggle in a non-allowlisted entity.
   High-impact domains (`HOMELAB_MCP_HA_CONFIRM_DOMAINS`: lock, alarm,
   cover, …) additionally require `confirm=true` and return a
   non-destructive preview without it. Every actuation reads the entity
   before AND after the call (polling up to the confirm timeout) and
   reports an honest `confirmed` flag — HA acknowledges a service call
   when it is *dispatched*, not when the device changed, so a tool must
   never report intent as outcome. Automation edits go ONLY through HA's
   config API (`/api/config/automation/config/<id>`), which validates and
   hot-reloads — this service must NEVER get filesystem access to HA's
   config directory (raw YAML write access is arbitrary-code-execution on
   the HA host via `shell_command` et al., and bypasses HA's validation).
   All writes emit an audit line on the `homelab_mcp.audit` logger
   (tool, target, args, outcome) because `RequestLogMiddleware` only sees
   `POST /mcp`. The HA token (`HOMELAB_MCP_HA_TOKEN`, sops-managed) never
   comes from user input and is never logged; prefer a dedicated HA user
   (admin only if the automation config-API tools are needed).

## Testing rules

- `tests/test_auth.py` is the canary — it uses a real RSA keypair and
  exercises every JWT rejection path. If you change `auth.py`, those
  tests must still pass.
- Save-recipe path-traversal tests live in `tests/test_tools_cooklang.py`.
- Tool-handler tests can mock the upstream HTTP calls with `pytest-httpx`.
  Don't hit real upstream services from CI.

## Style

- `ruff format` then `ruff check` then `mypy src` before committing.
  CI enforces all three.
- All public functions: type-annotated, with a one-line docstring.
- Tool decorators: use `name` + `description`. Skip `annotations.title`
  for now (newer MCP SDK feature; client support is uneven).

## Versioning

- Bump `version` in `pyproject.toml` AND `src/homelab_mcp/__init__.py`
  together. CI doesn't enforce this yet — be deliberate.
- Tag releases with `v0.x.y`. Consumers (nix-config) pin to a specific
  commit via `inputs.homelab-mcp.url = "github:carpenike/mcp/<sha>"`,
  not a tag, so the tag is mostly for humans.

## What this project is NOT

- Not multi-tenant. Single user (you). Per-user authorization gates can
  go inside individual tools by reading `scope["user"]["email"]` but
  there's no roles concept and no plan to add one.
- Not a general-purpose MCP framework. The patterns here work for "small
  Python service that wraps a handful of HTTP APIs"; don't reach for them
  for "complex MCP server with sessions / streams / long-running tools".
- Not a deployment target for arbitrary tools. Each tool category should
  have a clear purpose and a clean upstream API. If a tool needs to be a
  shell wrapper, that's a signal it belongs in a different service that
  this one calls.
