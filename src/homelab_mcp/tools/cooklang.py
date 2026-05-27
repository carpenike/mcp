"""Cooklang recipe tools.

Wraps three upstream services on forge:

  - cook.holthome.net          (CookCLI web server — personal recipes + shopping list)
  - fedcook.holthome.net       (cooklang-federation — search across 62 indexed feeds)
  - /data/cooklang/recipes/    (filesystem — `save_recipe` writes here)

Tool name convention: `cooklang_<verb>_<object>`. See AGENTS.md.
"""

from __future__ import annotations

import logging
import re
from pathlib import Path
from typing import TYPE_CHECKING, Annotated, Any, Literal

import httpx
from pydantic import Field

if TYPE_CHECKING:
    from mcp.server.fastmcp import FastMCP

    from homelab_mcp.config import Settings

log = logging.getLogger(__name__)

# Recipe-name regex — alphanumeric, hyphens, underscores. Deliberately
# strict: keeps `save_recipe` immune to path traversal AND ensures the
# saved file is greppable / referenceable consistently across the
# cooklang ecosystem (which generally assumes kebab-case filenames).
NAME_RE = re.compile(r"^[a-zA-Z0-9_-]+$")

# Loose sanity check that a payload looks like a Cooklang `.cook` file.
# Cooklang files commonly start with metadata (`>>`), comments (`--`),
# headers (`==`), step text (any letter), or ingredients (`@`). We
# accept any non-whitespace first character — the real protection is
# `save_recipe` writes under `claude/`, so even a malformed file is
# contained and easy to delete.
_CONTENT_MAX_BYTES = 256 * 1024

# Bind ContextVar at module load — Starlette doesn't currently propagate
# scope into the FastMCP tool function, so the per-request claims are
# stashed by JWTAuthMiddleware on scope["user"] and we read them via
# ASGI context where available. For v0.1 we don't enforce per-user
# gating in tool handlers; we surface this lookup for future use.


def register(mcp: FastMCP, settings: Settings) -> None:
    """Register cooklang_* tools on the given MCP server."""
    cookcli = settings.cooklang_base_url.rstrip("/")
    fedurl = settings.federation_base_url.rstrip("/")
    recipes_dir = Path(settings.recipes_dir)
    claude_subdir = recipes_dir / "claude"

    # ── search ──────────────────────────────────────────────────────
    @mcp.tool(
        name="cooklang_search_recipes",
        description=(
            "Search the federated recipe index by free-text query. The "
            "index covers your personal cookbook (carpenike/recipes) and "
            "~60 community feeds. Returns matching recipes with id, "
            "title, and tags. Follow up with `cooklang_get_recipe` "
            "(source='federation') to fetch the full recipe. Use this "
            "for 'find me a recipe for X' style questions."
        ),
    )
    async def search_recipes(
        query: Annotated[str, Field(description="Free-text search query")],
        limit: Annotated[
            int,
            Field(ge=1, le=50, description="Maximum results to return"),
        ] = 10,
    ) -> dict[str, Any]:
        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.get(
                f"{fedurl}/api/search",
                params={"q": query, "limit": limit},
            )
            resp.raise_for_status()
            return resp.json()  # type: ignore[no-any-return]

    # ── get one recipe ──────────────────────────────────────────────
    @mcp.tool(
        name="cooklang_get_recipe",
        description=(
            "Fetch a recipe's full structured contents (ingredients, "
            "steps, metadata, timing). Use source='federation' with the "
            "id from `cooklang_search_recipes` to get any indexed recipe; "
            "use source='mine' with the name from `cooklang_list_my_recipes` "
            "to get your personal recipes (which have richer metadata "
            "since they're on the same filesystem as the CookCLI server)."
        ),
    )
    async def get_recipe(
        identifier: Annotated[
            str,
            Field(description="Recipe id (federation) or name without .cook (mine)"),
        ],
        source: Annotated[
            Literal["federation", "mine"],
            Field(description="Where to look up the recipe"),
        ] = "federation",
    ) -> dict[str, Any]:
        async with httpx.AsyncClient(timeout=15) as client:
            if source == "federation":
                resp = await client.get(f"{fedurl}/api/recipes/{identifier}")
            else:
                resp = await client.get(f"{cookcli}/api/recipes/{identifier}")
            resp.raise_for_status()
            return resp.json()  # type: ignore[no-any-return]

    # ── list personal cookbook ──────────────────────────────────────
    @mcp.tool(
        name="cooklang_list_my_recipes",
        description=(
            "List all recipes in your personal cookbook at "
            "/data/cooklang/recipes. Returns the directory tree. Useful "
            "for discovering what recipes you have before planning a meal "
            "or building a shopping list. The tree includes hand-curated "
            "recipes at the root and Claude-saved ones under 'claude/'."
        ),
    )
    async def list_my_recipes() -> dict[str, Any]:
        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.get(f"{cookcli}/api/recipes")
            resp.raise_for_status()
            return resp.json()  # type: ignore[no-any-return]

    # ── shopping list ───────────────────────────────────────────────
    @mcp.tool(
        name="cooklang_build_shopping_list",
        description=(
            "Generate a shopping list by combining ingredients from one or "
            "more of YOUR recipes (must be in /data/cooklang/recipes). "
            "Ingredients are aggregated and grouped by store aisle (produce, "
            "dairy, meat, pantry, spices) based on your aisle.conf. Pass "
            "recipe names without the .cook extension as returned by "
            "`cooklang_list_my_recipes`. Use for 'what do I need from the "
            "store for X, Y, and Z' workflows."
        ),
    )
    async def build_shopping_list(
        recipe_names: Annotated[
            list[str],
            Field(description="Recipe names from your personal cookbook (no .cook extension)"),
        ],
    ) -> dict[str, Any]:
        # CookCLI's POST /api/shopping_list accepts a JSON array of recipe
        # paths. Body format may evolve with cookcli upstream — adjust here
        # if a deploy hits a 400.
        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.post(
                f"{cookcli}/api/shopping_list",
                json=recipe_names,
            )
            resp.raise_for_status()
            return resp.json()  # type: ignore[no-any-return]

    # ── save recipe ─────────────────────────────────────────────────
    @mcp.tool(
        name="cooklang_save_recipe",
        description=(
            "Save a new .cook recipe to your personal cookbook, under the "
            "`claude/` subdirectory so it's clearly separated from your "
            "hand-curated recipes. The downstream pipeline carries the new "
            "recipe everywhere automatically: Resilio Sync propagates it "
            "to your other devices within seconds; cooklang-git-sync "
            "commits it to github.com/carpenike/recipes within 15 minutes; "
            "the federation re-indexes it within ~6 hours. Refuses to "
            "overwrite an existing file unless `overwrite=True`. Use this "
            "after `cooklang_get_recipe` (source='federation') to bookmark "
            "a community recipe into your personal collection."
        ),
    )
    async def save_recipe(
        name: Annotated[
            str,
            Field(
                description=(
                    "Recipe name (alphanumeric, hyphens, underscores only; "
                    "no path separators or extensions)"
                ),
            ),
        ],
        content: Annotated[
            str,
            Field(description="Full .cook file content (Cooklang markup)"),
        ],
        overwrite: Annotated[
            bool,
            Field(description="Replace an existing recipe with the same name"),
        ] = False,
    ) -> dict[str, Any]:
        # 1. Validate name strictly (path-traversal guard).
        if not NAME_RE.match(name):
            return {
                "error": "invalid recipe name",
                "reason": "allowed characters: [a-zA-Z0-9_-]",
                "received": name,
            }

        # 2. Size cap.
        encoded = content.encode()
        if len(encoded) > _CONTENT_MAX_BYTES:
            return {
                "error": "recipe too large",
                "limit_bytes": _CONTENT_MAX_BYTES,
                "received_bytes": len(encoded),
            }

        # 3. Sanity check the payload looks like a recipe (loose).
        if not content.strip():
            return {"error": "empty content"}

        # 4. Ensure subdir exists. The NixOS module pre-creates this with
        #    mode 02770 (group=cooklang, setgid) at deploy time, but
        #    handle the case where deploy ordering left it missing.
        try:
            claude_subdir.mkdir(parents=True, exist_ok=True)
        except OSError as e:
            return {
                "error": "could not create claude/ subdirectory",
                "path": str(claude_subdir),
                "reason": str(e),
            }

        # 5. Resolve target and re-check it stayed inside the subdir
        #    (defence-in-depth on top of NAME_RE).
        target = (claude_subdir / f"{name}.cook").resolve()
        try:
            target.relative_to(claude_subdir.resolve())
        except ValueError:
            return {
                "error": "resolved path escaped claude/ subdir",
                "path": str(target),
            }

        # 6. Overwrite policy.
        if target.exists() and not overwrite:
            return {
                "error": "recipe already exists",
                "path": f"claude/{name}.cook",
                "hint": "pass overwrite=True to replace",
            }

        # 7. Write.
        try:
            target.write_text(content, encoding="utf-8")
        except OSError as e:
            return {
                "error": "write failed",
                "path": str(target),
                "reason": str(e),
            }

        log.info("saved recipe: claude/%s.cook (%d bytes)", name, len(encoded))
        return {
            "ok": True,
            "saved": f"claude/{name}.cook",
            "bytes_written": len(encoded),
            "next_steps": (
                "Resilio sync propagates to peers within seconds. "
                "cooklang-git-sync commits + pushes within 15 min. "
                "Federation re-indexes within ~6 h."
            ),
        }
