"""ROM library tools (FileBrowser Quantum).

Read-only discovery over the ROM storage served by FileBrowser Quantum
(gtsteffaniak/filebrowser). The library is one directory per system at the
share root (snes/, n64/, switch/, ...), mostly No-Intro-named files, with
optional region subfolders inside a system. Quantum maintains a real-time
index of the share, so `roms_search` is instant even at ~65k files.

Authentication: the FileBrowser account can't mint long-lived API keys
(its `api` permission is off), so this module performs Quantum's login
handshake itself — `POST /api/auth/login?username=<u>` with the
URL-encoded password in an `X-Password` header — and caches the returned
session JWT (~2h TTL), re-authenticating shortly before expiry and once
on an unexpected 401. The password comes from `Settings.roms_password`
(sops-managed env var), never from user input, and is never logged.

Tool name convention: `roms_<verb>_<object>`. See AGENTS.md.
"""

from __future__ import annotations

import asyncio
import base64
import binascii
import json as jsonlib
import logging
import re
import time
from typing import TYPE_CHECKING, Annotated, Any
from urllib.parse import quote, urlencode

import httpx
from mcp.types import ToolAnnotations
from pydantic import Field

from homelab_mcp.tools._http import ToolError, make_client, request_json

if TYPE_CHECKING:
    from mcp.server.fastmcp import FastMCP

    from homelab_mcp.config import Settings

log = logging.getLogger(__name__)

_TIMEOUT = 20.0

# Refresh the cached session token this many seconds before its `exp`.
_TOKEN_SLACK_SECONDS = 60.0

# Filenames follow No-Intro conventions: "Title (Region) (Rev 1) (En,Fr).ext".
_TAG_RE = re.compile(r"\(([^()]*)\)")

# System directory names as observed at the share root (lowercase alnum).
_SYSTEM_RE = re.compile(r"^[a-z0-9][a-z0-9 ._-]{0,63}$")

# claude.ai's permission UI groups tools by these hints; every roms_*
# tool is a pure read of a fixed internal service (see AGENTS.md).
_RO = ToolAnnotations(
    readOnlyHint=True, destructiveHint=False, idempotentHint=True, openWorldHint=False
)

_UNCONFIGURED_HINT = (
    "Set HOMELAB_MCP_ROMS_BASE_URL, HOMELAB_MCP_ROMS_USERNAME and "
    "HOMELAB_MCP_ROMS_PASSWORD (password via the sops EnvironmentFile)."
)


def _clean_path(path: str) -> str:
    """Validate a client-supplied library path and normalize it to '/x/y' form.

    Paths travel as a query parameter (never a URL path segment), so this
    guards the upstream filesystem semantics: no backslashes, no NULs, no
    `..` segments (AGENTS rule 3).
    """
    if "\\" in path or "\0" in path:
        raise ToolError("roms_invalid_path", "Path contains forbidden characters.", "")
    if not path.startswith("/"):
        path = "/" + path
    if any(seg == ".." for seg in path.split("/")):
        raise ToolError("roms_invalid_path", "Path must not contain '..' segments.", "")
    return path


def _jwt_exp(token: str) -> float | None:
    """Best-effort read of the `exp` claim from an (unverified) JWT."""
    parts = token.split(".")
    if len(parts) != 3:
        return None
    try:
        payload = parts[1] + "=" * (-len(parts[1]) % 4)
        claims = jsonlib.loads(base64.urlsafe_b64decode(payload))
        exp = claims.get("exp")
        return float(exp) if exp is not None else None
    except (ValueError, binascii.Error):
        return None


def _parse_name(filename: str) -> dict[str, Any]:
    """Split a No-Intro-style filename into title + parenthesized tags."""
    stem, _, ext = filename.rpartition(".")
    if not stem:  # no dot at all
        stem, ext = filename, ""
    tags = _TAG_RE.findall(stem)
    title = _TAG_RE.sub("", stem).strip().rstrip("-").strip()
    # Switch dumps use "[titleid][version]" brackets rather than parens.
    bracket_tags = re.findall(r"\[([^\[\]]*)\]", title)
    if bracket_tags:
        tags.extend(bracket_tags)
        title = re.sub(r"\[[^\[\]]*\]", "", title).strip()
    return {"title": title, "tags": tags, "extension": ext.lower()}


def _file_entry(name: str, item: dict[str, Any]) -> dict[str, Any]:
    """Project one upstream file record into the tool output shape."""
    return {
        "name": name,
        "size_bytes": item.get("size"),
        "content_type": item.get("type"),
        "modified": item.get("modified"),
        **_parse_name(name),
    }


class _FbSession:
    """Cached FileBrowser Quantum session token with re-login on expiry."""

    def __init__(self, client: httpx.AsyncClient, base: str, username: str, password: str) -> None:
        self._client = client
        self._base = base
        self._username = username
        self._password = password
        self._token = ""
        self._exp = 0.0
        self._lock = asyncio.Lock()

    async def token(self, *, force: bool = False) -> str:
        """Return a live session token, logging in if missing/stale/forced."""
        async with self._lock:
            if force or not self._token or time.time() >= self._exp - _TOKEN_SLACK_SECONDS:
                await self._login()
            return self._token

    async def _login(self) -> None:
        # Quantum's login endpoint: username + recaptcha as query params,
        # password URL-encoded in the X-Password header (what the web UI
        # sends). The 200 body is the bare JWT as text/plain.
        try:
            resp = await self._client.post(
                f"{self._base}/api/auth/login",
                params={"username": self._username, "recaptcha": ""},
                headers={"X-Password": quote(self._password, safe="")},
            )
        except httpx.HTTPError as exc:
            log.warning("roms login failed: %s", exc.__class__.__name__)
            raise ToolError(
                "roms_unreachable",
                f"Could not reach the ROM storage ({exc.__class__.__name__}).",
                "Check HOMELAB_MCP_ROMS_BASE_URL and that FileBrowser is up.",
            ) from exc
        if resp.status_code != 200:
            raise ToolError(
                "roms_auth_failed",
                f"FileBrowser rejected the login (HTTP {resp.status_code}).",
                "Check HOMELAB_MCP_ROMS_USERNAME / HOMELAB_MCP_ROMS_PASSWORD.",
            )
        token = resp.text.strip()
        if not token:
            raise ToolError("roms_auth_failed", "FileBrowser returned an empty token.", "")
        self._token = token
        self._exp = _jwt_exp(token) or (time.time() + 300.0)


def register(mcp: FastMCP, settings: Settings) -> None:
    """Register roms_* ROM-library tools on the given MCP server."""
    base = settings.roms_base_url.rstrip("/")
    source = settings.roms_source
    configured = bool(base and settings.roms_username and settings.roms_password)

    # ONE pooled client for the module (see _http.make_client).
    client = make_client(timeout=_TIMEOUT)
    session = _FbSession(client, base, settings.roms_username, settings.roms_password)

    async def _call(path: str, params: dict[str, Any]) -> Any:
        """One authenticated GET against the FileBrowser API.

        Retries exactly once with a fresh login if the cached token is
        rejected (revoked server-side, clock skew, restart...).
        """
        if not configured:
            raise ToolError(
                "roms_unreachable", "The ROM storage is not configured.", _UNCONFIGURED_HINT
            )
        token = await session.token()
        for attempt in (1, 2):
            try:
                return await request_json(
                    client,
                    "GET",
                    f"{base}/api{path}",
                    service="roms",
                    params=params,
                    headers={"Authorization": f"Bearer {token}"},
                    unreachable_hint="Check HOMELAB_MCP_ROMS_BASE_URL and that FileBrowser is up.",
                )
            except ToolError as err:
                if attempt == 1 and err.code == "roms_http_401":
                    token = await session.token(force=True)
                    continue
                raise
        return None  # pragma: no cover — loop always returns or raises

    # ── list systems ────────────────────────────────────────────────
    @mcp.tool(
        annotations=_RO,
        name="roms_list_systems",
        description=(
            "List the game systems (consoles) in the ROM library — one "
            "directory per system (snes, n64, gamecube, wii, wiiu, switch, "
            "genesis, ...) with total size and last-modified time. Start "
            "here to learn what platforms exist before browsing or to "
            "answer 'what systems do we have ROMs for?'."
        ),
    )
    async def list_systems() -> dict[str, Any]:
        try:
            data = await _call("/resources", {"path": "/", "source": source})
        except ToolError as err:
            return err.payload()
        systems = [
            {
                "system": f.get("name"),
                "size_bytes": f.get("size"),
                "modified": f.get("modified"),
            }
            for f in (data or {}).get("folders") or []
        ]
        return {"total": len(systems), "systems": systems}

    # ── browse a directory ──────────────────────────────────────────
    @mcp.tool(
        annotations=_RO,
        name="roms_browse",
        description=(
            "List one directory of the ROM library: subfolders and ROM "
            "files (name, size, type, modified, plus the parsed No-Intro "
            "title and tags like region/revision). Paths are '/'-rooted at "
            "the library root, e.g. '/snes/' or '/snes/SNES Japanese ROMs'. "
            "Use after roms_list_systems to explore a system; use "
            "roms_search instead when looking for a specific game."
        ),
    )
    async def browse(
        path: Annotated[
            str,
            Field(description="Directory path from the library root, e.g. '/snes/'"),
        ] = "/",
        limit: Annotated[int, Field(ge=1, le=500)] = 200,
    ) -> dict[str, Any]:
        try:
            clean = _clean_path(path)
            data = await _call("/resources", {"path": clean, "source": source})
        except ToolError as err:
            return err.payload()
        if (data or {}).get("type") != "directory":
            entry = _file_entry((data or {}).get("name") or clean, data or {})
            return {"path": clean, "file": entry}
        folders = [
            {"name": f.get("name"), "size_bytes": f.get("size"), "modified": f.get("modified")}
            for f in data.get("folders") or []
        ]
        all_files = data.get("files") or []
        files = [_file_entry(f.get("name") or "", f) for f in all_files[:limit]]
        return {
            "path": clean,
            "folders": folders,
            "files": files,
            "returned": len(files),
            "total": len(all_files),
            "truncated": len(all_files) > len(files),
        }

    # ── search the whole library ────────────────────────────────────
    @mcp.tool(
        annotations=_RO,
        name="roms_search",
        description=(
            "Search the entire ROM library by name (FileBrowser's real-time "
            "index — fast across all ~65k files). Returns matching files "
            "with their system, path, size and parsed No-Intro title/tags. "
            "Optionally restrict to one system (as returned by "
            "roms_list_systems). Use for 'do we have <game>?' or 'which "
            "versions of <game> are there?'. Query must be at least 3 "
            "characters (server-side minimum)."
        ),
    )
    async def search(
        query: Annotated[str, Field(min_length=3, description="Free-text filename search")],
        system: Annotated[
            str | None,
            Field(description="Optional system directory to scope to, e.g. 'snes'"),
        ] = None,
        limit: Annotated[int, Field(ge=1, le=200)] = 50,
    ) -> dict[str, Any]:
        params: dict[str, Any] = {"query": query, "sources": source}
        if system is not None:
            sysname = system.strip().strip("/").lower()
            if not _SYSTEM_RE.match(sysname):
                return ToolError(
                    "roms_invalid_system",
                    f"Not a valid system name: {system!r}.",
                    "Use a directory name from roms_list_systems, e.g. 'snes'.",
                ).payload()
            params["scope"] = f"/{sysname}/"
        try:
            results = await _call("/tools/search", params)
        except ToolError as err:
            return err.payload()
        hits = []
        for r in (results or [])[:limit]:
            rpath = (r.get("path") or "").lstrip("/")
            first_seg, _, _rest = rpath.partition("/")
            name = rpath.rsplit("/", 1)[-1]
            hits.append(
                {
                    "path": "/" + rpath,
                    "system": first_seg or None,
                    **_file_entry(name, r),
                }
            )
        total = len(results or [])
        return {
            "query": query,
            "system": system,
            "results": hits,
            "returned": len(hits),
            "total": total,
            "truncated": total > len(hits),
        }

    # ── download URL for one file ───────────────────────────────────
    @mcp.tool(
        annotations=_RO,
        name="roms_get_download_url",
        description=(
            "Verify one ROM file exists and return a direct download URL "
            "for it, plus its size and type. The URL is served by the "
            "FileBrowser instance and requires being logged in to it in "
            "the browser (the link carries no credentials). Use the exact "
            "path from roms_search or roms_browse."
        ),
    )
    async def get_download_url(
        path: Annotated[
            str,
            Field(
                description="File path from the library root, e.g. '/snes/Secret of Mana (USA).sfc'"
            ),
        ],
    ) -> dict[str, Any]:
        try:
            clean = _clean_path(path)
            data = await _call("/resources", {"path": clean, "source": source})
        except ToolError as err:
            return err.payload()
        if (data or {}).get("type") == "directory":
            return ToolError(
                "roms_not_a_file",
                f"{clean} is a directory, not a file.",
                "Pass a file path from roms_browse or roms_search.",
            ).payload()
        qs = urlencode({"file": clean, "source": source})
        return {
            "path": clean,
            "size_bytes": (data or {}).get("size"),
            "content_type": (data or {}).get("type"),
            "modified": (data or {}).get("modified"),
            "download_url": f"{base}/api/resources/download?{qs}",
            "note": (
                "Open while logged in to FileBrowser in the browser; the "
                "URL itself carries no credentials."
            ),
        }
