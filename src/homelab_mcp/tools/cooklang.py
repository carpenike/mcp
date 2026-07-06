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
    GET /api/recipes/<relpath>?format=raw -> the raw `.cook` source text
                                   (frontmatter + body) as text/plain. Used
                                   to expose the authored source and to reuse
                                   it for metadata-only updates. If the server
                                   ignores the param and returns JSON we treat
                                   the source as unavailable rather than leak
                                   the JSON envelope.

  WRITE
    PUT /api/recipes/<relpath>  -> writes a `.cook` file. The server APPENDS
                                   `.cook` itself, so <relpath> must NOT
                                   include the extension. Silently OVERWRITES
                                   (no create-vs-update distinction), so we
                                   GET-check for collisions ourselves.
    DELETE /api/recipes/<relpath> -> removes a `.cook` file.

All upstream calls go through `_http` (long-lived pooled client + guarded
JSON decode) so a proxy/SSO 200-HTML page can never throw to the transport,
and every failure surfaces as the shared ``{"error": {code, message, hint}}``
shape (matching grocy/gatus).

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

from homelab_mcp.tools._http import ToolError, make_client, request_json

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

# Frontmatter KEYS we are willing to write. Strict identifier set so a
# caller-supplied metadata dict can never inject additional YAML lines
# (e.g. a key containing a newline + a second `key: value`).
_META_KEY_RE = re.compile(r"^[A-Za-z0-9_-]+$")

# Cap on the body of an authored recipe.
_CONTENT_MAX_BYTES = 256 * 1024

# Bound on how many recipes we will fetch when an ingredient-level search
# is explicitly requested (match_ingredients=True). Keeps a deep search from
# fanning out into hundreds of requests.
_MAX_INGREDIENT_SCAN = 200
_INGREDIENT_FETCH_CONCURRENCY = 8

# Subdirectory new authored recipes land in by default, keeping
# machine-authored recipes visibly separate from hand-curated ones.
_DEFAULT_AUTHOR_FOLDER = "claude"

# Slug marker for the throwaway parser-validation files. Any leaf carrying
# it is a validation artifact (possibly orphaned by a cleanup failure) and
# must never appear in a listing.
_TEMP_MARKER = "zz-tmp-"

# One request timeout for the whole module (writes need a little headroom).
_TIMEOUT = 30.0


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
    """Flatten the recursive recipe tree into a list of leaf summaries.

    Throwaway validation artifacts (``zz-tmp-`` slugs) are filtered out so an
    orphaned temp file can never surface in a listing.
    """
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
                rel = _abs_to_relpath(path, root)
                if _TEMP_MARKER not in rel:
                    meta = _normalize_metadata(recipe.get("metadata"))
                    out.append(
                        {
                            "slug": meta.get("id"),
                            "title": meta.get("title") or child.get("name"),
                            "path": rel,
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


def _validate_frontmatter_input(metadata: dict[str, Any] | None, *scalars: Any) -> ToolError | None:
    """Reject caller-supplied frontmatter that could inject or corrupt YAML.

    Guards the write path against frontmatter injection: user-supplied metadata
    KEYS must be a strict identifier set (so a key can't smuggle a newline and a
    second ``key: value`` line), and any string VALUE — including standalone
    scalars like ``title`` — must not carry newlines or other control characters
    (which would spill into adjacent frontmatter lines / produce ambiguous YAML).

    Only validates caller input, never metadata already stored on the server, so
    curated recipes with unusual existing keys keep updating fine.
    """

    def bad_value(candidate: Any) -> bool:
        return isinstance(candidate, str) and any(ord(ch) < 32 for ch in candidate)

    for scalar in scalars:
        if bad_value(scalar):
            return ToolError(
                "invalid_metadata_value",
                "A metadata value contains newlines or control characters.",
                "remove newlines / control characters from the value",
            )
    if metadata:
        for key, value in metadata.items():
            if not isinstance(key, str) or not _META_KEY_RE.match(key):
                return ToolError(
                    "invalid_metadata_key",
                    f"Metadata key {key!r} is not allowed.",
                    "metadata keys must match [A-Za-z0-9_-]",
                )
            items = value if isinstance(value, list | tuple) else [value]
            if any(bad_value(item) for item in items):
                return ToolError(
                    "invalid_metadata_value",
                    f"Metadata value for {key!r} contains newlines or control characters.",
                    "remove newlines / control characters from the value",
                )
    return None


def _build_cook_document(metadata: dict[str, Any], body: str) -> str:
    """Assemble a complete `.cook` file: YAML frontmatter + CookLang body."""
    frontmatter = _yaml_frontmatter(metadata)
    return f"---\n{frontmatter}\n---\n\n{body.strip(chr(10))}\n"


def _strip_frontmatter(source: str) -> str:
    """Return the CookLang body of a raw `.cook` source (frontmatter removed)."""
    m = re.match(r"^---\n.*?\n---\n?", source, re.S)
    return source[m.end() :] if m else source


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

    # ONE long-lived, connection-pooled client reused across every tool
    # invocation (TLS handshakes/sockets are pooled instead of rebuilt per
    # call). httpx binds it to the running loop on first request.
    client = make_client(timeout=_TIMEOUT)

    unreachable = "check cook.holthome.net is reachable"

    # ── low-level wire helpers (transport errors -> ToolError; JSON decode
    #    guarded so a 200 HTML/SSO page never throws to the transport) ────
    async def _request(
        method: str,
        url: str,
        *,
        content: bytes | None = None,
        headers: dict[str, str] | None = None,
        params: dict[str, Any] | None = None,
    ) -> httpx.Response:
        try:
            return await client.request(
                method, url, content=content, headers=headers, params=params
            )
        except httpx.HTTPError as exc:
            log.warning("cooklang %s %s failed: %s", method, url, exc.__class__.__name__)
            raise ToolError(
                "cooklang_unreachable",
                f"Could not reach cooklang ({exc.__class__.__name__}).",
                unreachable,
            ) from exc

    def _decode(resp: httpx.Response) -> Any:
        """Decode a response body as JSON, returning None on empty/non-JSON."""
        if not resp.content:
            return None
        try:
            return resp.json()
        except ValueError:
            return None

    async def _fetch_tree() -> dict[str, Any]:
        data = await request_json(
            client, "GET", f"{cook}/api/recipes", service="cooklang", unreachable_hint=unreachable
        )
        return data if isinstance(data, dict) else {}

    async def _fetch_recipe(relpath: str) -> httpx.Response:
        return await _request("GET", f"{cook}/api/recipes/{_enc(relpath)}")

    async def _fetch_source(relpath: str) -> str | None:
        """Fetch the raw `.cook` source text, or None if unavailable.

        Degrades gracefully: on any transport/HTTP failure — or if the server
        ignores ``?format=raw`` and returns the parsed JSON envelope — we return
        None rather than leaking JSON as the "source".
        """
        try:
            resp = await _request(
                "GET", f"{cook}/api/recipes/{_enc(relpath)}", params={"format": "raw"}
            )
        except ToolError:
            return None
        if resp.status_code != 200 or not resp.content:
            return None
        if "application/json" in resp.headers.get("content-type", "").lower():
            return None
        return resp.text

    async def _resolve_to_path(identifier: str) -> str | None:
        """Resolve a slug-or-path identifier to a concrete relpath, or None."""
        candidate = _sanitize_relpath(identifier)
        if candidate is not None:
            resp = await _fetch_recipe(candidate)
            if resp.status_code == 200:
                return candidate
        # Fall back to slug lookup via the tree (the `id` frontmatter slug
        # does not resolve a direct GET).
        tree = await _fetch_tree()
        for entry in _flatten_tree(tree, root):
            if entry["slug"] == identifier or entry["path"] == identifier:
                return str(entry["path"])
        return None

    async def _put_recipe(relpath: str, content: str) -> httpx.Response:
        return await _request(
            "PUT",
            f"{cook}/api/recipes/{_enc(relpath)}",
            content=content.encode("utf-8"),
            headers={"Content-Type": "text/plain; charset=utf-8"},
        )

    async def _delete_recipe(relpath: str) -> httpx.Response:
        return await _request("DELETE", f"{cook}/api/recipes/{_enc(relpath)}")

    async def _validate_via_temp(content: str) -> tuple[bool, dict[str, Any] | None]:
        """Round-trip `content` through the CookLang parser on a throwaway path.

        Writes to a temp path, reads it back, deletes it, and reports whether the
        server parsed it into real recipe content. Never touches the caller's
        target path, so a malformed document can never clobber a real recipe.
        The temp DELETE is retried once; the ``zz-tmp-`` slug is filtered from
        listings anyway, so an orphan is invisible if cleanup still fails.
        """
        temp = f"{_DEFAULT_AUTHOR_FOLDER}/{_TEMP_MARKER}{uuid.uuid4().hex}"
        try:
            put = await _put_recipe(temp, content)
            if put.status_code != 200:
                return False, None
            got = await _fetch_recipe(temp)
            parsed = _decode(got) if got.status_code == 200 else None
            ok = got.status_code == 200 and _parsed_ok(parsed)
            return ok, parsed if ok else None
        finally:
            cleaned = False
            for _ in range(2):
                try:
                    resp = await _delete_recipe(temp)
                except ToolError:
                    continue
                if resp.status_code in (200, 204, 404):
                    cleaned = True
                    break
            if not cleaned:
                log.warning("failed to clean up temp recipe %s", temp)

    async def _scan_ingredients(
        candidates: list[dict[str, Any]], tokens: list[str]
    ) -> list[dict[str, Any]]:
        """Fetch candidate recipes concurrently; return those whose ingredients match."""
        sem = asyncio.Semaphore(_INGREDIENT_FETCH_CONCURRENCY)

        async def check(entry: dict[str, Any]) -> dict[str, Any] | None:
            async with sem:
                try:
                    resp = await _fetch_recipe(str(entry["path"]))
                except ToolError:
                    return None
                if resp.status_code != 200:
                    return None
                data = _decode(resp)
            recipe = data.get("recipe", {}) if isinstance(data, dict) else {}
            names = " ".join(
                str(i.get("name") or "") for i in recipe.get("ingredients", [])
            ).lower()
            hits = sum(1 for tok in tokens if tok in names)
            return {**entry, "_score": hits} if hits else None

        found = await asyncio.gather(*(check(e) for e in candidates))
        return [r for r in found if r is not None]

    # ── list / search (merged) ──────────────────────────────────────
    @mcp.tool(
        name="cooklang_list_recipes",
        description=(
            "List and search YOUR canonical cookbook on cook.holthome.net — the "
            "authoritative inventory other services (Marginalia, Whiskey's Mess "
            "Hall) defer to. With no arguments it returns every recipe as "
            "{slug, title, path, course, cuisine, tags}. Narrow with `course`, "
            "`cuisine`, or `tag` filters. Pass a free-text `query` to rank matches "
            "over title / slug / tags / course / cuisine; set match_ingredients=True "
            "to also match ingredient names (slower — fetches recipe details). "
            "Returns {returned, total, truncated, recipes}: `total` is the full "
            "match count, `recipes` is truncated to `limit`, and `truncated` says "
            "whether more matched than were returned. Follow up with "
            "`cooklang_get_recipe` using a returned slug or path for full "
            "ingredients and steps. For discovery across ~60 community feeds "
            "instead of your own cookbook, use `cooklang_search_federation`."
        ),
    )
    async def list_recipes(
        query: Annotated[
            str | None,
            Field(description="Free-text query; ranks over title/slug/tags/course/cuisine"),
        ] = None,
        course: Annotated[
            str | None, Field(description="Filter to this course (e.g. 'dessert')")
        ] = None,
        cuisine: Annotated[
            str | None, Field(description="Filter to this cuisine (e.g. 'french')")
        ] = None,
        tag: Annotated[str | None, Field(description="Filter to recipes carrying this tag")] = None,
        match_ingredients: Annotated[
            bool,
            Field(description="Also match on ingredient names (slower — fetches recipe details)"),
        ] = False,
        limit: Annotated[int, Field(ge=1, le=500, description="Max recipes to return")] = 200,
    ) -> dict[str, Any]:
        try:
            tree = await _fetch_tree()
            recipes = _flatten_tree(tree, root)

            # Metadata pre-filters.
            filtered: list[dict[str, Any]] = []
            for entry in recipes:
                if course and (entry.get("course") or "").lower() != course.lower():
                    continue
                if cuisine and (entry.get("cuisine") or "").lower() != cuisine.lower():
                    continue
                tags = [str(t).lower() for t in (entry.get("tags") or [])]
                if tag and tag.lower() not in tags:
                    continue
                filtered.append(entry)

            tokens = [t for t in query.lower().split() if t] if query else []
            if tokens:
                matched: list[dict[str, Any]] = []
                unmatched: list[dict[str, Any]] = []
                for entry in filtered:
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
                    matched.extend(
                        await _scan_ingredients(unmatched[:_MAX_INGREDIENT_SCAN], tokens)
                    )
                matched.sort(key=lambda r: (-int(r.get("_score", 0)), str(r.get("title") or "")))
                selected = matched
            else:
                selected = filtered
        except ToolError as exc:
            return exc.payload()

        total = len(selected)
        out = [
            {
                "slug": r["slug"],
                "title": r["title"],
                "path": r["path"],
                "course": r["course"],
                "cuisine": r["cuisine"],
                "tags": r["tags"],
            }
            for r in selected[:limit]
        ]
        return {"returned": len(out), "total": total, "truncated": total > limit, "recipes": out}

    # ── get one recipe ──────────────────────────────────────────────
    @mcp.tool(
        name="cooklang_get_recipe",
        description=(
            "Fetch one recipe's full contents from cook.holthome.net: normalized "
            "metadata, the parsed ingredients / cookware / steps (the CookLang "
            "AST), AND the raw `.cook` `source` text (frontmatter + body, comments "
            "and formatting preserved). Pass the `source` straight back to "
            "`cooklang_update_recipe` as `body` to edit without losing formatting. "
            "Accepts either the recipe slug (the frontmatter `id`, e.g. "
            "'calvados-glazed-pork-belly-bites') or its relative path (e.g. "
            "'Smoker/Calvados-Glazed Pork Belly Bites'). Use after "
            "`cooklang_list_recipes` to read a specific recipe."
        ),
    )
    async def get_recipe(
        identifier: Annotated[
            str,
            Field(description="Recipe slug (frontmatter id) or relative path (no .cook)"),
        ],
    ) -> dict[str, Any]:
        try:
            relpath = await _resolve_to_path(identifier)
            if relpath is None:
                return ToolError(
                    "recipe_not_found", f"No recipe found for {identifier!r}.", ""
                ).payload()
            resp = await _fetch_recipe(relpath)
            if resp.status_code != 200:
                return ToolError(
                    "recipe_not_found", f"No recipe found for {identifier!r}.", ""
                ).payload()
            data = _decode(resp)
            source = await _fetch_source(relpath)
        except ToolError as exc:
            return exc.payload()

        recipe = data.get("recipe") if isinstance(data, dict) else None
        if not isinstance(recipe, dict):
            return ToolError(
                "unexpected_response",
                f"cooklang returned an unexpected shape for {identifier!r}.",
                "",
            ).payload()
        meta = _normalize_metadata(recipe.get("metadata"))
        return {
            "slug": meta.get("id"),
            "title": meta.get("title"),
            "path": relpath,
            "metadata": meta,
            "source": source,
            "ingredients": recipe.get("ingredients", []),
            "cookware": recipe.get("cookware", []),
            "steps": recipe.get("steps", []),
            "image": data.get("image") if isinstance(data, dict) else None,
        }

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
            "Supply arbitrary frontmatter via `metadata` (keys must be "
            "[A-Za-z0-9_-]; values may not contain newlines); `derived_from` (and "
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
            return ToolError(
                "invalid_slug",
                f"Slug {final_slug!r} is not allowed.",
                "allowed characters: [a-zA-Z0-9_-]",
            ).payload()

        folder_clean = _sanitize_relpath(folder) if folder else _DEFAULT_AUTHOR_FOLDER
        if folder_clean is None:
            return ToolError(
                "invalid_folder", f"Folder {folder!r} is not a valid recipe folder.", ""
            ).payload()
        relpath = f"{folder_clean}/{final_slug}"

        if not body.strip():
            return ToolError("empty_body", "The recipe body is empty.", "").payload()
        meta_err = _validate_frontmatter_input(metadata, title, derived_from, derived_via)
        if meta_err is not None:
            return meta_err.payload()

        meta = _assemble_metadata(title, final_slug, metadata, derived_from, derived_via)
        content = _build_cook_document(meta, body)

        encoded = content.encode("utf-8")
        if len(encoded) > _CONTENT_MAX_BYTES:
            return ToolError(
                "recipe_too_large",
                f"Recipe is {len(encoded)} bytes; limit is {_CONTENT_MAX_BYTES}.",
                "",
            ).payload()

        try:
            # Collision check — PUT overwrites silently, so we guard here.
            existing = await _fetch_recipe(relpath)
            if existing.status_code == 200 and not overwrite:
                return ToolError(
                    "recipe_exists",
                    f"A recipe already exists at {relpath}.",
                    "pass overwrite=True to replace, or choose a different slug",
                ).payload()

            ok, _ = await _validate_via_temp(content)
            if not ok:
                return ToolError(
                    "validation_failed",
                    "The recipe failed CookLang parser validation.",
                    "check @ingredient{}, #cookware{}, ~timer{} and frontmatter syntax",
                ).payload()

            put = await _put_recipe(relpath, content)
            if put.status_code != 200:
                return ToolError(
                    "write_failed", f"Write returned HTTP {put.status_code} for {relpath}.", ""
                ).payload()

            verify = await _fetch_recipe(relpath)
            verified = verify.status_code == 200 and _parsed_ok(_decode(verify))
        except ToolError as exc:
            return exc.payload()

        if not verified:
            return ToolError(
                "verification_failed", f"Post-write verification failed for {relpath}.", ""
            ).payload()

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
            "(or relative path). Supply a new CookLang `body` (same syntax as "
            "cooklang_create_recipe — wrap multi-word @ingredient / #cookware names "
            "in braces), OR omit `body` for a metadata-only update: the recipe's "
            "current raw source is fetched and reused so comments/formatting are "
            "preserved. Metadata is merged over the recipe's current frontmatter "
            "(pass `metadata`, `title`, `derived_from`, or `derived_via`; new keys "
            "must be [A-Za-z0-9_-] with newline-free values). The recipe must "
            "already exist — this never creates one. To RELOCATE or RENAME at the "
            "same time, pass `new_folder` and/or `new_slug`; the recipe is written "
            "at the new path and the old file is removed only AFTER the new one is "
            "verified. The amended document is round-trip validated through the "
            "CookLang parser on a throwaway path BEFORE the real recipe is written, "
            "so a malformed edit can never break the existing recipe."
        ),
    )
    async def update_recipe(
        slug: Annotated[
            str, Field(description="Slug (frontmatter id) or relative path of the recipe to amend")
        ],
        body: Annotated[
            str | None,
            Field(
                description=(
                    "New CookLang body (replaces the steps). Wrap multi-word "
                    "@ingredient / #cookware names in braces, e.g. @pork belly{1%kg}. "
                    "Omit for a metadata-only update that reuses the current source."
                )
            ),
        ] = None,
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
        new_folder: Annotated[
            str | None,
            Field(description="Relocate the recipe into this category folder (move)"),
        ] = None,
        new_slug: Annotated[
            str | None,
            Field(description="Rename the recipe's file + frontmatter id to this kebab-case slug"),
        ] = None,
        overwrite: Annotated[
            bool,
            Field(description="Allow a move/rename to overwrite an existing recipe at the target"),
        ] = False,
    ) -> dict[str, Any]:
        if body is not None and not body.strip():
            return ToolError("empty_body", "The recipe body is empty.", "").payload()
        if new_slug is not None and not NAME_RE.match(new_slug):
            return ToolError(
                "invalid_slug", f"Slug {new_slug!r} is not allowed.", "allowed: [a-zA-Z0-9_-]"
            ).payload()
        meta_err = _validate_frontmatter_input(metadata, title, derived_from, derived_via)
        if meta_err is not None:
            return meta_err.payload()
        dest_folder: str | None = None
        if new_folder is not None:
            dest_folder = _sanitize_relpath(new_folder)
            if dest_folder is None:
                return ToolError(
                    "invalid_folder", f"Folder {new_folder!r} is not a valid recipe folder.", ""
                ).payload()

        try:
            relpath = await _resolve_to_path(slug)
            if relpath is None:
                return ToolError("recipe_not_found", f"No recipe found for {slug!r}.", "").payload()

            current = await _fetch_recipe(relpath)
            if current.status_code != 200:
                return ToolError("recipe_not_found", f"No recipe found for {slug!r}.", "").payload()
            current_data = _decode(current)
            current_meta = _normalize_metadata(
                current_data.get("recipe", {}).get("metadata")
                if isinstance(current_data, dict)
                else None
            )

            # Resolve the body: caller-supplied, or reuse the current source
            # (formatting-preserving metadata-only update).
            if body is None:
                source = await _fetch_source(relpath)
                if source is None:
                    return ToolError(
                        "body_required",
                        "Could not fetch the current recipe source to reuse.",
                        "supply `body` explicitly",
                    ).payload()
                effective_body = _strip_frontmatter(source)
            else:
                effective_body = body

            merged = dict(current_meta)
            if metadata:
                merged.update(metadata)
            if title is not None:
                merged["title"] = title
            if derived_from is not None:
                merged["derived_from"] = derived_from
            if derived_via is not None:
                merged["derived_via"] = derived_via
            if new_slug is not None:
                merged["id"] = new_slug

            # Resolve the destination: keep the source folder/filename unless a
            # move (new_folder) or rename (new_slug) was asked for.
            src_folder, _, src_name = relpath.rpartition("/")
            final_folder = dest_folder if dest_folder is not None else src_folder
            final_name = new_slug if new_slug is not None else src_name
            dest_relpath = f"{final_folder}/{final_name}" if final_folder else final_name
            is_move = dest_relpath != relpath

            content = _build_cook_document(merged, effective_body)
            if len(content.encode("utf-8")) > _CONTENT_MAX_BYTES:
                return ToolError(
                    "recipe_too_large", f"Recipe exceeds {_CONTENT_MAX_BYTES} bytes.", ""
                ).payload()

            if is_move and not overwrite:
                collision = await _fetch_recipe(dest_relpath)
                if collision.status_code == 200:
                    return ToolError(
                        "destination_exists",
                        f"A recipe already exists at {dest_relpath}.",
                        "pass overwrite=true to replace it",
                    ).payload()

            ok, _ = await _validate_via_temp(content)
            if not ok:
                return ToolError(
                    "validation_failed",
                    "The recipe failed CookLang parser validation.",
                    "check @ingredient{}, #cookware{}, ~timer{} and frontmatter syntax",
                ).payload()

            put = await _put_recipe(dest_relpath, content)
            if put.status_code != 200:
                return ToolError(
                    "write_failed",
                    f"Write returned HTTP {put.status_code} for {dest_relpath}.",
                    "",
                ).payload()

            verify = await _fetch_recipe(dest_relpath)
            verified = verify.status_code == 200 and _parsed_ok(_decode(verify))

            # Remove the old file ONLY after the relocated copy verifies, so a
            # failed move never loses the original.
            if verified and is_move:
                deleted = await _delete_recipe(relpath)
                if deleted.status_code not in (200, 204):
                    return ToolError(
                        "move_incomplete",
                        f"Wrote {dest_relpath} but failed to remove the original {relpath} "
                        f"(HTTP {deleted.status_code}).",
                        "delete the original manually if it lingers",
                    ).payload()
        except ToolError as exc:
            return exc.payload()

        if not verified:
            return ToolError(
                "verification_failed", f"Post-write verification failed for {dest_relpath}.", ""
            ).payload()

        log.info(
            "updated recipe: %s%s",
            dest_relpath,
            f" (moved from {relpath})" if is_move else "",
        )
        result: dict[str, Any] = {"ok": True, "slug": merged.get("id"), "path": dest_relpath}
        if is_move:
            result["moved_from"] = relpath
        return result

    # ── delete ──────────────────────────────────────────────────────
    @mcp.tool(
        name="cooklang_delete_recipe",
        description=(
            "Delete an EXISTING recipe from cook.holthome.net, identified by slug "
            "(or relative path). This is DESTRUCTIVE and permanent. As a guard, "
            "it does nothing unless you pass `confirm=true`: called without it, "
            "the tool resolves the identifier and returns a preview of exactly "
            "which recipe (title + path) WOULD be deleted, so you can show the "
            "user and get explicit approval before calling again with "
            "confirm=true. Downstream services (Marginalia, Whiskey's Mess Hall) "
            "store only slugs and defer to this server, so deleting here removes "
            "the source of truth for that recipe."
        ),
    )
    async def delete_recipe(
        slug: Annotated[
            str,
            Field(description="Slug (frontmatter id) or relative path of the recipe to delete"),
        ],
        confirm: Annotated[
            bool,
            Field(
                description=(
                    "Must be true to actually delete. When false (default), the "
                    "tool returns a non-destructive preview of what would be "
                    "deleted instead of deleting it."
                )
            ),
        ] = False,
    ) -> dict[str, Any]:
        try:
            relpath = await _resolve_to_path(slug)
            if relpath is None:
                return ToolError("recipe_not_found", f"No recipe found for {slug!r}.", "").payload()

            # Read the title for a human-meaningful preview/confirmation.
            current = await _fetch_recipe(relpath)
            if current.status_code != 200:
                return ToolError("recipe_not_found", f"No recipe found for {slug!r}.", "").payload()
            current_data = _decode(current)
            meta = _normalize_metadata(
                current_data.get("recipe", {}).get("metadata")
                if isinstance(current_data, dict)
                else None
            )
            title = meta.get("title")

            if not confirm:
                return {
                    "ok": False,
                    "requires_confirmation": True,
                    "path": relpath,
                    "slug": meta.get("id"),
                    "title": title,
                    "hint": "re-call with confirm=true to permanently delete this recipe",
                }

            deleted = await _delete_recipe(relpath)
            if deleted.status_code not in (200, 204):
                return ToolError(
                    "delete_failed",
                    f"Delete returned HTTP {deleted.status_code} for {relpath}.",
                    "",
                ).payload()

            # Confirm it's gone: a follow-up GET must now 404.
            verify = await _fetch_recipe(relpath)
            if verify.status_code != 404:
                return ToolError(
                    "verification_failed",
                    f"Post-delete verification failed for {relpath} (HTTP {verify.status_code}).",
                    "",
                ).payload()
        except ToolError as exc:
            return exc.payload()

        log.info("deleted recipe: %s", relpath)
        return {"ok": True, "deleted": relpath, "slug": meta.get("id"), "title": title}

    # ── federation search (distinct: ~60 community feeds) ───────────
    @mcp.tool(
        name="cooklang_search_federation",
        description=(
            "Search the federated recipe index (~60 community feeds plus your "
            "own repo) by free-text query. Returns matching recipes with id, "
            "title, and tags from across the wider cooklang community — use this "
            "for 'find me a recipe for X' discovery when your own cookbook "
            "doesn't have it. For YOUR canonical recipes only, use "
            "`cooklang_list_recipes`."
        ),
    )
    async def search_federation(
        query: Annotated[str, Field(description="Free-text search query")],
        limit: Annotated[int, Field(ge=1, le=50, description="Maximum results to return")] = 10,
    ) -> dict[str, Any]:
        try:
            result: Any = await request_json(
                client,
                "GET",
                f"{fed}/api/search",
                service="cooklang",
                params={"q": query, "limit": limit},
                unreachable_hint="check fedcook.holthome.net is reachable",
            )
        except ToolError as exc:
            return exc.payload()
        return result if isinstance(result, dict) else {"results": result}

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
            result: Any = await request_json(
                client,
                "POST",
                f"{cook}/api/shopping_list",
                service="cooklang",
                json=recipe_names,
                unreachable_hint=unreachable,
            )
        except ToolError as exc:
            return exc.payload()
        return result if isinstance(result, dict) else {"items": result}
