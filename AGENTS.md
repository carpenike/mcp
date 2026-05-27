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

1. Files starting with `_` are ignored (e.g. `_registry.py`).
2. Each module gets its own `register(mcp, settings)` function — keep
   tool definitions inside that closure so they can capture URLs from
   `settings`.
3. Always validate inputs at the tool boundary. Path-like inputs need
   strict regexes (see `cooklang.NAME_RE`).
4. Never raise to the MCP transport — catch upstream errors and return
   `{"error": "..."}` payloads. Claude can read those and adapt.
5. Tool descriptions matter — they're what Claude uses to decide which
   tool to call. Be specific about WHEN to use the tool, not just what
   it does.

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
2. **No tool exposes filesystem writes outside an explicit allowlist.**
   `cooklang_save_recipe` is the only writer right now, and it's
   confined to `/data/cooklang/recipes/claude/` with a strict
   `NAME_RE = [a-zA-Z0-9_-]+` regex.
3. **No tool shells out without explicit input sanitisation.** Today no
   tool shells out at all. If a future tool does, it MUST use `subprocess`
   with `shell=False` and a fully validated argv list.
4. **Upstream URLs come from `Settings`, never from user input.** A tool
   that took a URL parameter and `httpx.get`'d it would be an SSRF vector.
5. **Tokens never reach logs.** `auth.py` logs `kid` and `iss` but never
   the token contents. Keep it that way.

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
