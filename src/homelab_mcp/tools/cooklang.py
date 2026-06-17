"""Cooklang recipe tools — the authoritative recipe surface.

Wraps the self-hosted CookLang server on forge:

  - cook.holthome.net          (canonical `.cook` recipes — read + author)
  - fedcook.holthome.net       (cooklang-federation — search ~60 community feeds)

cook.holthome.net is the source of truth for recipe *content*. These tools
are the read/author surface that downstream services (Marginalia — which
stores only slugs — and Whiskey's Mess Hall) call. We WRAP the CookLang
server's HTTP API; we never fork it and never hold recipe content ourselves.

Empirically-verified wire (probed against cook.holthome.net 2026-06-17):

  READ
    GET /api/recipes            -> recursive tree; each leaf carries
                                   recipe.metadata as a FLAT dict.
    GET /api/recipes/<relpath>  -> parsed `.cook` (ingredients / cookware /
                                   steps) with metadata WRAPPED as
                                   {"map": {...}}. <relpath> is relative to
                                   the recipe root, spaces as %20, literal
                                   `/` for subdirs, `.cook` extension
                                   optional. The frontmatter `id` slug does
                                   NOT resolve a GET — you must use the path.

  WRITE
    PUT /api/recipes/<relpath>  -> writes a `.cook` file. The server APPENDS
                                   `.cook` itself, so <relpath> must NOT
                                   include the extension. Silently OVERWRITES
                                   (no create-vs-update distinction), so we
                                   GET-check for collisions ourselves.
    DELETE /api/recipes/<relpath> -> removes a `.cook` file.

Tool name convention: `cooklang_<verb>_<object>`. See AGENTS.md.
"""

from __future__ import annotations

import asyncio
import logging
import re
import uuid
from typing import TYPE_CHECKING, Annotated, Any
from urllib.parse import quote

import httpx
from pydantic import Field

if TYPE_CHECKING:
    from mcp.server.fastmcp import FastMCP

    from homelab_mcp.config import Settings

log = logging.getLogger(__name__)

# Slug / created-filename regex — strict kebab-safe set. Deliberately
# narrow: it is the filename we hand to the CookLang server's PUT, so it
# must be immune to path traversal AND consistent with the kebab-case
# `id` convention the cooklang ecosystem assumes.
NAME_RE = re.compile(r"^[a-zA-Z0-9_-]+$")

# Folder-segment regex for EXISTING category directories (e.g. "Smoker",
# "Appetizers", "Confit & Smoked Beef Tongue"). More permissive than
# NAME_RE because curated folders/files use spaces, ampersands, parens and
# apostrophes — but still forbids `/`, `\`, `.`, NUL and other traversal
# vectors (a segment containing `.` — and therefore `..` — is rejected).
_PATH_SEGMENT_RE = re.compile(r"^[A-Za-z0-9 _&()'-]+$")

# Cap on the body of an authored recipe.
_CONTENT_MAX_BYTES = 256 * 1024

# Bound on how many recipes we will fetch when an ingredient-level search
# is explicitly requested (search_recipes match_ingredients=True). Keeps a
# deep search from fanning out into hundreds of requests.
_MAX_INGREDIENT_SCAN = 200
_INGREDIENT_FETCH_CONCURRENCY = 8

# Subdirectory new authored recipes land in by default, keeping
# machine-authored recipes visibly separate from hand-curated ones.
_DEFAULT_AUTHOR_FOLDER = "claude"


# ── pure helpers (no I/O — unit-tested directly) ────────────────────────
def _normalize_metadata(meta: Any) -> dict[str, Any]:
    """Return recipe metadata as a flat dict.

    The tree endpoint returns metadata flat; the single-recipe endpoint
    wraps it as ``{"map": {...}}``. Normalize both to a flat dict.
    """
    if not isinstance(meta, dict):
        return {}
    inner = meta.get("map")
    if isinstance(inner, dict):
        return inner
    return meta


def _abs_to_relpath(abs_path: str, root: str) -> str:
    """Convert an absolute tree path to the relpath the API expects.

    Strips the recipe-root prefix and the trailing ``.cook`` extension
    (the server adds the extension back on every read/write).
    """
    root = root.rstrip("/")
    path = abs_path
    if path.startswith(root + "/"):
        path = path[len(root) + 1 :]
    if path.endswith(".cook"):
        path = path[: -len(".cook")]
    return path


def _flatten_tree(tree: dict[str, Any], root: str) -> list[dict[str, Any]]:
    """Flatten the recursive recipe tree into a list of leaf summaries."""
    out: list[dict[str, Any]] = []

    def walk(node: dict[str, Any]) -> None:
        children = node.get("children")
        if not isinstance(children, dict):
            return
        for child in children.values():
            if not isinstance(child, dict):
                continue
            recipe = child.get("recipe")
            path = child.get("path")
            if isinstance(recipe, dict) and isinstance(path, str) and path.endswith(".cook"):
                meta = _normalize_metadata(recipe.get("metadata"))
                out.append(
                    {
                        "slug": meta.get("id"),
                        "title": meta.get("title") or child.get("name"),
                        "path": _abs_to_relpath(path, root),
                        "course": meta.get("course"),
                        "cuisine": meta.get("cuisine"),
                        "tags": meta.get("tags") or [],
                        "metadata": meta,
                    }
                )
            walk(child)

    walk(tree)
    return out


def _slugify(title: str) -> str:
    """Derive a kebab-case slug from a free-text title."""
    slug = re.sub(r"[^a-z0-9]+", "-", title.strip().lower())
    return slug.strip("-")


def _sanitize_relpath(path: str) -> str | None:
    """Validate a caller-supplied relpath; return the clean form or None.

    Rejects absolute paths, traversal (`..`), backslashes, NUL bytes, and
    any segment with characters outside the permissive folder set. The
    ``.cook`` extension is stripped if present (the server re-adds it).
    """
    if not path or "\x00" in path or "\\" in path:
        return None
    cleaned = path.strip().strip("/")
    if cleaned.endswith(".cook"):
        cleaned = cleaned[: -len(".cook")]
    if not cleaned:
        return None
    segments = cleaned.split("/")
    for seg in segments:
        if not seg or not _PATH_SEGMENT_RE.match(seg):
            return None
    return "/".join(segments)


def _yaml_scalar(value: Any) -> str:
    """Serialize a scalar to a safe (double-quoted where needed) YAML token."""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int | float):
        return str(value)
    text = str(value)
    escaped = text.replace("\\", "\\\\").replace('"', '\\"')
    return f'"{escaped}"'


def _yaml_frontmatter(metadata: dict[str, Any]) -> str:
    """Serialize a metadata dict to YAML frontmatter (scalars + scalar lists)."""
    lines: list[str] = []
    for key, value in metadata.items():
        if value is None:
            continue
        if isinstance(value, list | tuple):
            if not value:
                continue
            lines.append(f"{key}:")
            lines.extend(f"  - {_yaml_scalar(item)}" for item in value)
        else:
            lines.append(f"{key}: {_yaml_scalar(value)}")
    return "\n".join(lines)


def _build_cook_document(metadata: dict[str, Any], body: str) -> str:
    """Assemble a complete `.cook` file: YAML frontmatter + CookLang body."""
    frontmatter = _yaml_frontmatter(metadata)
    return f"---\n{frontmatter}\n---\n\n{body.strip(chr(10))}\n"


def _parsed_ok(data: Any) -> bool:
    """True if a single-recipe GET payload parsed into real recipe content."""
    if not isinstance(data, dict):
        return False
    recipe = data.get("recipe")
    if not isinstance(recipe, dict):
        return False
    meta = _normalize_metadata(recipe.get("metadata"))
    has_content = bool(recipe.get("ingredients") or recipe.get("steps") or recipe.get("cookware"))
    return bool(meta) and has_content


def _enc(relpath: str) -> str:
    """Percent-encode a relpath for the API URL (spaces -> %20, keep `/`)."""
    return quote(relpath, safe="/")


def _assemble_metadata(
    title: str,
    slug: str,
    extra: dict[str, Any] | None,
    derived_from: str | None,
    derived_via: str | None,
) -> dict[str, Any]:
    """Build the frontmatter dict, keeping title/id first and lineage explicit."""
    meta: dict[str, Any] = {"title": title, "id": slug}
    if extra:
        for key, value in extra.items():
            if key in ("title", "id"):
                continue
            meta[key] = value
    if derived_from is not None:
        meta["derived_from"] = derived_from
    if derived_via is not None:
        meta["derived_via"] = derived_via
    return meta


def register(mcp: FastMCP, settings: Settings) -> None:
    """Register cooklang_* tools on the given MCP server."""
    cook = settings.cooklang_base_url.rstrip("/")
    fed = settings.federation_base_url.rstrip("/")
    root = settings.recipes_dir.rstrip("/")

    # ── shared I/O helpers (capture base URLs from settings) ────────
    async def _fetch_tree(client: httpx.AsyncClient) -> dict[str, Any]:
        resp = await client.get(f"{cook}/api/recipes")
        resp.raise_for_status()
        data: dict[str, Any] = resp.json()
        return data

    async def _fetch_recipe(client: httpx.AsyncClient, relpath: str) -> httpx.Response:
        return await client.get(f"{cook}/api/recipes/{_enc(relpath)}")

    async def _resolve_to_path(client: httpx.AsyncClient, identifier: str) -> str | None:
        """Resolve a slug-or-path identifier to a concrete relpath, or None."""
        candidate = _sanitize_relpath(identifier)
        if candidate is not None:
            resp = await _fetch_recipe(client, candidate)
            if resp.status_code == 200:
                return candidate
        # Fall back to slug lookup via the tree (the `id` frontmatter slug
        # does not resolve a direct GET).
        tree = await _fetch_tree(client)
        for entry in _flatten_tree(tree, root):
            if entry["slug"] == identifier or entry["path"] == identifier:
                return str(entry["path"])
        return None

    async def _put_recipe(client: httpx.AsyncClient, relpath: str, content: str) -> httpx.Response:
        return await client.put(
            f"{cook}/api/recipes/{_enc(relpath)}",
            content=content.encode("utf-8"),
            headers={"Content-Type": "text/plain; charset=utf-8"},
        )

    async def _delete_recipe(client: httpx.AsyncClient, relpath: str) -> httpx.Response:
        return await client.delete(f"{cook}/api/recipes/{_enc(relpath)}")

    async def _validate_via_temp(
        client: httpx.AsyncClient, content: str
    ) -> tuple[bool, dict[str, Any] | None]:
        """Round-trip `content` through the CookLang parser on a throwaway path.

        Writes to a temp path, reads it back, deletes it, and reports
        whether the server parsed it into real recipe content. Never
        touches the caller's target path, so a malformed document can
        never clobber a real recipe.
        """
        temp = f"{_DEFAULT_AUTHOR_FOLDER}/zz-tmp-{uuid.uuid4().hex}"
        try:
            put = await _put_recipe(client, temp, content)
            if put.status_code != 200:
                return False, None
            got = await _fetch_recipe(client, temp)
            parsed = got.json() if got.status_code == 200 else None
            ok = got.status_code == 200 and _parsed_ok(parsed)
            return ok, parsed if ok else None
        finally:
            try:
                await _delete_recipe(client, temp)
            except httpx.HTTPError:
                log.warning("failed to clean up temp recipe %s", temp)

    async def _scan_ingredients(
        client: httpx.AsyncClient, candidates: list[dict[str, Any]], tokens: list[str]
    ) -> list[dict[str, Any]]:
        """Fetch candidate recipes concurrently; return those whose ingredients match."""
        sem = asyncio.Semaphore(_INGREDIENT_FETCH_CONCURRENCY)

        async def check(entry: dict[str, Any]) -> dict[str, Any] | None:
            async with sem:
                try:
                    resp = await _fetch_recipe(client, str(entry["path"]))
                    if resp.status_code != 200:
                        return None
                    recipe = resp.json().get("recipe", {})
                except (httpx.HTTPError, ValueError):
                    return None
            names = " ".join(
                str(i.get("name") or "") for i in recipe.get("ingredients", [])
            ).lower()
            hits = sum(1 for tok in tokens if tok in names)
            return {**entry, "_score": hits} if hits else None

        found = await asyncio.gather(*(check(e) for e in candidates))
        return [r for r in found if r is not None]

    # ── list ────────────────────────────────────────────────────────
    @mcp.tool(
        name="cooklang_list_recipes",
        description=(
            "List recipes from the canonical CookLang server (cook.holthome.net) "
            "as a flat array of {slug, title, path, course, cuisine, tags}. This "
            "is the authoritative inventory of YOUR cookbook — the source of "
            "truth other services (Marginalia, Whiskey's Mess Hall) defer to. "
            "Optional filters narrow by course, cuisine, tag, or a free-text "
            "substring over title/slug/tags. Follow up with `cooklang_get_recipe` "
            "using a returned slug or path to fetch full ingredients and steps."
        ),
    )
    async def list_recipes(
        course: Annotated[
            str | None, Field(description="Filter to this course (e.g. 'dessert')")
        ] = None,
        cuisine: Annotated[
            str | None, Field(description="Filter to this cuisine (e.g. 'french')")
        ] = None,
        tag: Annotated[str | None, Field(description="Filter to recipes carrying this tag")] = None,
        query: Annotated[
            str | None,
            Field(description="Free-text substring over title / slug / tags"),
        ] = None,
        limit: Annotated[int, Field(ge=1, le=500, description="Max recipes to return")] = 200,
    ) -> dict[str, Any]:
        try:
            async with httpx.AsyncClient(timeout=20) as client:
                tree = await _fetch_tree(client)
        except httpx.HTTPError as exc:
            return {"error": "failed to list recipes", "reason": str(exc)}

        recipes = _flatten_tree(tree, root)
        q = query.lower().strip() if query else None
        out: list[dict[str, Any]] = []
        for entry in recipes:
            if course and (entry.get("course") or "").lower() != course.lower():
                continue
            if cuisine and (entry.get("cuisine") or "").lower() != cuisine.lower():
                continue
            tags = [str(t).lower() for t in (entry.get("tags") or [])]
            if tag and tag.lower() not in tags:
                continue
            if q:
                haystack = " ".join(
                    [str(entry.get("title") or ""), str(entry.get("slug") or ""), *tags]
                ).lower()
                if q not in haystack:
                    continue
            out.append(
                {
                    "slug": entry["slug"],
                    "title": entry["title"],
                    "path": entry["path"],
                    "course": entry["course"],
                    "cuisine": entry["cuisine"],
                    "tags": entry["tags"],
                }
            )

        return {"count": len(out), "total": len(recipes), "recipes": out[:limit]}

    # ── get one recipe ──────────────────────────────────────────────
    @mcp.tool(
        name="cooklang_get_recipe",
        description=(
            "Fetch one recipe's full structured contents from cook.holthome.net: "
            "normalized metadata plus the parsed ingredients, cookware, and steps "
            "(the CookLang AST). Accepts either the recipe slug (the frontmatter "
            "`id`, e.g. 'calvados-glazed-pork-belly-bites') or its relative path "
            "(e.g. 'Smoker/Calvados-Glazed Pork Belly Bites'). Use this after "
            "`cooklang_list_recipes` or `cooklang_search_recipes` to read a "
            "specific recipe."
        ),
    )
    async def get_recipe(
        identifier: Annotated[
            str,
            Field(description="Recipe slug (frontmatter id) or relative path (no .cook)"),
        ],
    ) -> dict[str, Any]:
        try:
            async with httpx.AsyncClient(timeout=20) as client:
                relpath = await _resolve_to_path(client, identifier)
                if relpath is None:
                    return {"error": "recipe not found", "identifier": identifier}
                resp = await _fetch_recipe(client, relpath)
                if resp.status_code != 200:
                    return {"error": "recipe not found", "identifier": identifier, "path": relpath}
                data = resp.json()
        except httpx.HTTPError as exc:
            return {"error": "failed to fetch recipe", "reason": str(exc)}

        recipe = data.get("recipe") if isinstance(data, dict) else None
        if not isinstance(recipe, dict):
            return {"error": "unexpected response shape", "identifier": identifier}
        meta = _normalize_metadata(recipe.get("metadata"))
        return {
            "slug": meta.get("id"),
            "title": meta.get("title"),
            "path": relpath,
            "metadata": meta,
            "ingredients": recipe.get("ingredients", []),
            "cookware": recipe.get("cookware", []),
            "steps": recipe.get("steps", []),
            "image": data.get("image"),
        }

    # ── search ──────────────────────────────────────────────────────
    @mcp.tool(
        name="cooklang_search_recipes",
        description=(
            "Search YOUR canonical cookbook (cook.holthome.net) by free-text "
            "query, matching on recipe name, slug, tags, course, and cuisine. "
            "Set match_ingredients=True to also match on ingredient names (this "
            "fetches recipe details and is slower). Returns ranked matches with "
            "slug/title/path. For discovering recipes across ~60 community feeds "
            "instead of your own, use `cooklang_search_federation`."
        ),
    )
    async def search_recipes(
        query: Annotated[str, Field(description="Free-text search query")],
        course: Annotated[str | None, Field(description="Restrict to this course")] = None,
        cuisine: Annotated[str | None, Field(description="Restrict to this cuisine")] = None,
        match_ingredients: Annotated[
            bool,
            Field(description="Also match on ingredient names (slower — fetches recipe details)"),
        ] = False,
        limit: Annotated[int, Field(ge=1, le=100, description="Max results")] = 20,
    ) -> dict[str, Any]:
        tokens = [t for t in query.lower().split() if t]
        if not tokens:
            return {"error": "empty query"}

        try:
            async with httpx.AsyncClient(timeout=20) as client:
                tree = await _fetch_tree(client)
                recipes = _flatten_tree(tree, root)

                # Apply metadata pre-filters.
                if course:
                    recipes = [
                        r for r in recipes if (r.get("course") or "").lower() == course.lower()
                    ]
                if cuisine:
                    recipes = [
                        r for r in recipes if (r.get("cuisine") or "").lower() == cuisine.lower()
                    ]

                matched: list[dict[str, Any]] = []
                unmatched: list[dict[str, Any]] = []
                for entry in recipes:
                    tags = [str(t).lower() for t in (entry.get("tags") or [])]
                    haystack = " ".join(
                        [
                            str(entry.get("title") or ""),
                            str(entry.get("slug") or ""),
                            str(entry.get("course") or ""),
                            str(entry.get("cuisine") or ""),
                            *tags,
                        ]
                    ).lower()
                    hits = sum(1 for tok in tokens if tok in haystack)
                    if hits:
                        matched.append({**entry, "_score": hits})
                    else:
                        unmatched.append(entry)

                if match_ingredients and unmatched:
                    ingredient_hits = await _scan_ingredients(
                        client, unmatched[:_MAX_INGREDIENT_SCAN], tokens
                    )
                    matched.extend(ingredient_hits)
        except httpx.HTTPError as exc:
            return {"error": "search failed", "reason": str(exc)}

        matched.sort(key=lambda r: (-int(r.get("_score", 0)), str(r.get("title") or "")))
        results = [
            {
                "slug": r["slug"],
                "title": r["title"],
                "path": r["path"],
                "course": r["course"],
                "cuisine": r["cuisine"],
                "tags": r["tags"],
            }
            for r in matched[:limit]
        ]
        return {"count": len(results), "query": query, "recipes": results}

    # ── create ──────────────────────────────────────────────────────
    @mcp.tool(
        name="cooklang_create_recipe",
        description=(
            "Author a NEW recipe and write it to cook.holthome.net as a valid "
            "`.cook` file (YAML frontmatter + CookLang body). CookLang body "
            "syntax: ingredients are @name and cookware is #name; quantities go "
            "in braces as {qty%unit} (e.g. @flour{200%g}); timers are ~{5%min} or "
            "~name{5%min}. CRITICAL: a multi-word ingredient or cookware name MUST "
            "be wrapped in braces or only the FIRST word is captured — write "
            "@pork belly{1%kg} and #cast iron pan{}, never @pork belly. Example "
            "step: 'Sear @pork belly{1%kg} in a #cast iron pan{} for ~{5%min}.' "
            "Supply arbitrary frontmatter via `metadata`; `derived_from` (and "
            "optional `derived_via`) are first-class args for copy-on-promote "
            "lineage — set derived_from to the slug of the baseline recipe a fork "
            "was promoted from (this is what powers Marginalia's lineage "
            "stitching). The slug defaults to a kebab-case form of the title and "
            "becomes the filename. FAILS if the slug/path already exists unless "
            "overwrite=True. The document is round-trip validated through the "
            "CookLang parser before it is committed. Returns the assigned "
            "slug and path."
        ),
    )
    async def create_recipe(
        title: Annotated[str, Field(description="Human-readable recipe title")],
        body: Annotated[
            str,
            Field(
                description=(
                    "CookLang body (one or more step lines). Wrap multi-word "
                    "@ingredient and #cookware names in braces — @pork belly{1%kg}, "
                    "#cast iron pan{} — else only the first word is captured. "
                    "Quantities: {qty%unit}. Timer: ~{5%min}. Example: "
                    "'Sear @pork belly{1%kg} in a #cast iron pan{} for ~{5%min}.'"
                )
            ),
        ],
        slug: Annotated[
            str | None,
            Field(description="Kebab-case slug/filename ([a-zA-Z0-9_-]); defaults from title"),
        ] = None,
        folder: Annotated[
            str | None,
            Field(description="Recipe-tree folder to write into; defaults to 'claude'"),
        ] = None,
        metadata: Annotated[
            dict[str, Any] | None,
            Field(description="Arbitrary extra frontmatter keys (course, cuisine, tags, ...)"),
        ] = None,
        derived_from: Annotated[
            str | None,
            Field(description="Slug of the baseline recipe this was promoted/forked from"),
        ] = None,
        derived_via: Annotated[
            str | None,
            Field(description="Optional note on how it was derived (e.g. 'calvados glaze swap')"),
        ] = None,
        overwrite: Annotated[
            bool, Field(description="Replace an existing recipe at the target path")
        ] = False,
    ) -> dict[str, Any]:
        final_slug = slug or _slugify(title)
        if not NAME_RE.match(final_slug):
            return {
                "error": "invalid slug",
                "reason": "allowed characters: [a-zA-Z0-9_-]",
                "received": final_slug,
            }

        folder_clean = _sanitize_relpath(folder) if folder else _DEFAULT_AUTHOR_FOLDER
        if folder_clean is None:
            return {"error": "invalid folder", "received": folder}
        relpath = f"{folder_clean}/{final_slug}"

        meta = _assemble_metadata(title, final_slug, metadata, derived_from, derived_via)
        content = _build_cook_document(meta, body)

        encoded = content.encode("utf-8")
        if len(encoded) > _CONTENT_MAX_BYTES:
            return {
                "error": "recipe too large",
                "limit_bytes": _CONTENT_MAX_BYTES,
                "received_bytes": len(encoded),
            }
        if not body.strip():
            return {"error": "empty body"}

        try:
            async with httpx.AsyncClient(timeout=30) as client:
                # Collision check — PUT overwrites silently, so we guard here.
                existing = await _fetch_recipe(client, relpath)
                if existing.status_code == 200 and not overwrite:
                    return {
                        "error": "recipe already exists",
                        "path": relpath,
                        "hint": "pass overwrite=True to replace, or choose a different slug",
                    }

                ok, _ = await _validate_via_temp(client, content)
                if not ok:
                    return {
                        "error": "recipe failed CookLang parser validation",
                        "hint": "check @ingredient{}, #cookware{}, ~timer{} and frontmatter syntax",
                    }

                put = await _put_recipe(client, relpath, content)
                if put.status_code != 200:
                    return {"error": "write failed", "status": put.status_code, "path": relpath}

                verify = await _fetch_recipe(client, relpath)
                verified = verify.status_code == 200 and _parsed_ok(verify.json())
        except httpx.HTTPError as exc:
            return {"error": "create failed", "reason": str(exc)}

        if not verified:
            return {"error": "post-write verification failed", "path": relpath}

        log.info("created recipe: %s (slug=%s)", relpath, final_slug)
        return {
            "ok": True,
            "slug": final_slug,
            "path": relpath,
            "derived_from": derived_from,
            "bytes_written": len(encoded),
        }

    # ── update ──────────────────────────────────────────────────────
    @mcp.tool(
        name="cooklang_update_recipe",
        description=(
            "Amend an EXISTING recipe on cook.holthome.net, identified by slug "
            "(or relative path). You supply the new CookLang `body` (same syntax "
            "as cooklang_create_recipe — wrap multi-word @ingredient / #cookware "
            "names in braces); metadata is "
            "merged over the recipe's current frontmatter (pass `metadata`, "
            "`title`, `derived_from`, or `derived_via` to change those keys). The "
            "recipe must already exist — this never creates one. The amended "
            "document is round-trip validated through the CookLang parser on a "
            "throwaway path BEFORE the real recipe is overwritten, so a malformed "
            "edit can never break the existing recipe."
        ),
    )
    async def update_recipe(
        slug: Annotated[
            str, Field(description="Slug (frontmatter id) or relative path of the recipe to amend")
        ],
        body: Annotated[
            str,
            Field(
                description=(
                    "New CookLang body (replaces the steps). Wrap multi-word "
                    "@ingredient / #cookware names in braces, e.g. @pork belly{1%kg}."
                )
            ),
        ],
        title: Annotated[str | None, Field(description="New title, if changing it")] = None,
        metadata: Annotated[
            dict[str, Any] | None,
            Field(description="Frontmatter keys to merge over the existing metadata"),
        ] = None,
        derived_from: Annotated[
            str | None, Field(description="Set/replace the derived_from lineage slug")
        ] = None,
        derived_via: Annotated[
            str | None, Field(description="Set/replace the derived_via note")
        ] = None,
    ) -> dict[str, Any]:
        if not body.strip():
            return {"error": "empty body"}

        try:
            async with httpx.AsyncClient(timeout=30) as client:
                relpath = await _resolve_to_path(client, slug)
                if relpath is None:
                    return {"error": "recipe not found", "identifier": slug}

                current = await _fetch_recipe(client, relpath)
                if current.status_code != 200:
                    return {"error": "recipe not found", "identifier": slug, "path": relpath}
                current_meta = _normalize_metadata(current.json().get("recipe", {}).get("metadata"))

                merged = dict(current_meta)
                if metadata:
                    merged.update(metadata)
                if title is not None:
                    merged["title"] = title
                if derived_from is not None:
                    merged["derived_from"] = derived_from
                if derived_via is not None:
                    merged["derived_via"] = derived_via

                content = _build_cook_document(merged, body)
                if len(content.encode("utf-8")) > _CONTENT_MAX_BYTES:
                    return {"error": "recipe too large", "limit_bytes": _CONTENT_MAX_BYTES}

                ok, _ = await _validate_via_temp(client, content)
                if not ok:
                    return {
                        "error": "recipe failed CookLang parser validation",
                        "hint": "check @ingredient{}, #cookware{}, ~timer{} and frontmatter syntax",
                    }

                put = await _put_recipe(client, relpath, content)
                if put.status_code != 200:
                    return {"error": "write failed", "status": put.status_code, "path": relpath}

                verify = await _fetch_recipe(client, relpath)
                verified = verify.status_code == 200 and _parsed_ok(verify.json())
        except httpx.HTTPError as exc:
            return {"error": "update failed", "reason": str(exc)}

        if not verified:
            return {"error": "post-write verification failed", "path": relpath}

        log.info("updated recipe: %s", relpath)
        return {"ok": True, "slug": merged.get("id"), "path": relpath}

    # ── federation search (distinct: ~60 community feeds) ───────────
    @mcp.tool(
        name="cooklang_search_federation",
        description=(
            "Search the federated recipe index (~60 community feeds plus your "
            "own repo) by free-text query. Returns matching recipes with id, "
            "title, and tags from across the wider cooklang community — use this "
            "for 'find me a recipe for X' discovery when your own cookbook "
            "doesn't have it. For YOUR canonical recipes only, use "
            "`cooklang_search_recipes`."
        ),
    )
    async def search_federation(
        query: Annotated[str, Field(description="Free-text search query")],
        limit: Annotated[int, Field(ge=1, le=50, description="Maximum results to return")] = 10,
    ) -> dict[str, Any]:
        try:
            async with httpx.AsyncClient(timeout=15) as client:
                resp = await client.get(f"{fed}/api/search", params={"q": query, "limit": limit})
                resp.raise_for_status()
                return resp.json()  # type: ignore[no-any-return]
        except httpx.HTTPError as exc:
            return {"error": "federation search failed", "reason": str(exc)}

    # ── shopping list ───────────────────────────────────────────────
    @mcp.tool(
        name="cooklang_build_shopping_list",
        description=(
            "Generate a shopping list by combining ingredients from one or more "
            "of YOUR recipes on cook.holthome.net. Ingredients are aggregated and "
            "grouped by store aisle. Pass recipe names/paths as returned by "
            "`cooklang_list_recipes`. Use for 'what do I need from the store for "
            "X, Y, and Z' workflows."
        ),
    )
    async def build_shopping_list(
        recipe_names: Annotated[
            list[str],
            Field(description="Recipe names/paths from your cookbook"),
        ],
    ) -> dict[str, Any]:
        try:
            async with httpx.AsyncClient(timeout=15) as client:
                resp = await client.post(f"{cook}/api/shopping_list", json=recipe_names)
                resp.raise_for_status()
                return resp.json()  # type: ignore[no-any-return]
        except httpx.HTTPError as exc:
            return {"error": "shopping list failed", "reason": str(exc)}
