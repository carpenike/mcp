"""ARC Raiders game-data tools.

Read-only lookups against three public community sources for the game
ARC Raiders (Embark Studios):

- **MetaForge** (`metaforge.app/api/arc-raiders`) — items, quests, live
  trader stock, and the rotating in-raid event schedule. Free, keyless,
  no SLA; attribution (link to metaforge.app/arc-raiders) is required by
  its docs, so every response carries a `source` field.
- **RaidTheory/arcraiders-data** — the MIT-licensed community JSON repo
  (raw.githubusercontent.com) that powers arctracker.io; used here for
  map metadata.
- **ARC Raiders Wiki** (`arcraiders.wiki`) — the Embark-supported
  MediaWiki; full Action API, content CC BY-SA 4.0. Used for free-text
  lookups (lore, weapon infobox stats, anything not in the datasets).

All tools are read-only projections; no upstream writes exist. Upstream
base URLs come from Settings (never user input, per AGENTS.md #4). User
input is only ever sent as query-string values (httpx-encoded) — nothing
user-supplied is interpolated into a URL path.

Fixed-URL responses (trader stock, event schedule, maps) are cached in a
tiny in-memory TTL cache so repeat calls don't hammer the free upstreams.
Parameterized searches are NOT cached — an unbounded query space must not
grow an unbounded cache.

Tool name convention: `arc_<verb>_<object>`. See AGENTS.md.
"""

from __future__ import annotations

import html
import logging
import re
import time
from typing import TYPE_CHECKING, Annotated, Any

from pydantic import Field

from homelab_mcp.tools._http import ToolError, make_client, request_json

if TYPE_CHECKING:
    from mcp.server.fastmcp import FastMCP

    from homelab_mcp.config import Settings

log = logging.getLogger(__name__)

METAFORGE_SOURCE = "MetaForge — https://metaforge.app/arc-raiders"
RAIDTHEORY_SOURCE = (
    "RaidTheory/arcraiders-data (MIT) — "
    "https://github.com/RaidTheory/arcraiders-data · https://arctracker.io"
)
WIKI_SOURCE = "ARC Raiders Wiki (CC BY-SA 4.0) — https://arcraiders.wiki"

# Cache TTLs (seconds). Live data (trader stock, event rotation) turns over
# on the order of hours; map metadata changes only on game patches.
TTL_LIVE = 900
TTL_STATIC = 21600

# Wiki pages can be long; cap what we return so one tool call can't flood
# the context window. Truncation is always flagged, never silent.
WIKI_TEXT_MAX = 8000

_TAG_RE = re.compile(r"<[^>]+>")


def _strip_tags(fragment: str) -> str:
    """Drop HTML tags and unescape entities from a wiki search snippet."""
    return html.unescape(_TAG_RE.sub("", fragment))


def _iso_utc(epoch_ms: Any) -> str | None:
    """Render a MetaForge epoch-milliseconds timestamp as ISO-8601 UTC."""
    if not isinstance(epoch_ms, (int, float)):
        return None
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(epoch_ms / 1000))


class _TTLCache:
    """Minimal per-module TTL cache for a fixed, known-small set of URLs."""

    def __init__(self) -> None:
        self._store: dict[str, tuple[float, Any]] = {}

    def get(self, key: str) -> Any | None:
        entry = self._store.get(key)
        if entry is None:
            return None
        expires, value = entry
        if time.monotonic() >= expires:
            del self._store[key]
            return None
        return value

    def put(self, key: str, value: Any, ttl: float) -> None:
        self._store[key] = (time.monotonic() + ttl, value)


def _project_item(raw: dict[str, Any]) -> dict[str, Any]:
    """Reduce a MetaForge item to the fields worth showing (stat zeros dropped)."""
    stats = {k: v for k, v in (raw.get("stat_block") or {}).items() if v}
    return {
        "id": raw.get("id"),
        "name": raw.get("name"),
        "type": raw.get("item_type"),
        "subcategory": raw.get("subcategory"),
        "rarity": raw.get("rarity"),
        "value": raw.get("value"),
        "description": raw.get("description"),
        "workbench": raw.get("workbench"),
        "stats": stats,
        "updated_at": raw.get("updated_at"),
    }


def _project_quest(raw: dict[str, Any]) -> dict[str, Any]:
    """Reduce a MetaForge quest to objectives, giver, and reward names."""
    rewards = [
        {
            "item": (r.get("item") or {}).get("name") or r.get("item_id"),
            "rarity": (r.get("item") or {}).get("rarity"),
            "quantity": r.get("quantity"),
        }
        for r in raw.get("rewards") or []
    ]
    guide = raw.get("guide_url")
    return {
        "id": raw.get("id"),
        "name": raw.get("name"),
        "trader": raw.get("trader_name"),
        "objectives": raw.get("objectives") or [],
        "xp": raw.get("xp"),
        "rewards": rewards,
        "guide_url": f"https://metaforge.app{guide}" if guide else None,
    }


def register(mcp: FastMCP, settings: Settings) -> None:
    """Register arc_* ARC Raiders game-data tools on the given MCP server."""
    metaforge = settings.arcraiders_metaforge_base_url.rstrip("/")
    data_base = settings.arcraiders_data_base_url.rstrip("/")
    wiki_api = settings.arcraiders_wiki_api_url
    # One pooled client for the lifetime of the process (see _http.make_client).
    # Three upstream hosts, so no base_url; MetaForge can be slow on cold cache.
    client = make_client(timeout=20.0)
    cache = _TTLCache()

    async def _cached_json(url: str, *, service: str, ttl: float, hint: str = "") -> Any:
        cached = cache.get(url)
        if cached is not None:
            return cached
        fresh = await request_json(client, "GET", url, service=service, unreachable_hint=hint)
        cache.put(url, fresh, ttl)
        return fresh

    # ── items ───────────────────────────────────────────────────────
    @mcp.tool(
        name="arc_search_items",
        description=(
            "Search the ARC Raiders item database (weapons, ammo, gear, "
            "materials, quick-use) by name. Returns per-item type, rarity, "
            "sell value, description, crafting workbench, and non-zero "
            "combat/utility stats. Use for 'what does X do', 'what is X "
            "worth', or 'which items match Y' questions. Item data comes "
            "from MetaForge and may lag the newest game patch."
        ),
    )
    async def search_items(
        query: Annotated[str, Field(min_length=1, description="Free-text item name search.")],
        limit: Annotated[int, Field(ge=1, le=25)] = 10,
        page: Annotated[int, Field(ge=1)] = 1,
    ) -> dict[str, Any]:
        try:
            raw = await request_json(
                client,
                "GET",
                f"{metaforge}/items",
                service="metaforge",
                params={"search": query, "limit": limit, "page": page},
                unreachable_hint="MetaForge may be down; try again shortly.",
            )
        except ToolError as err:
            return err.payload()
        items = [_project_item(i) for i in (raw.get("data") or [])]
        pagination = raw.get("pagination") or {}
        total = pagination.get("total", len(items))
        return {
            "query": query,
            "items": items,
            "returned": len(items),
            "total": total,
            "truncated": bool(pagination.get("hasNextPage")),
            "source": METAFORGE_SOURCE,
        }

    # ── quests ──────────────────────────────────────────────────────
    @mcp.tool(
        name="arc_search_quests",
        description=(
            "Search ARC Raiders quests by name (empty query lists all, "
            "paginated). Returns each quest's giver (trader), objectives, "
            "XP, item rewards, and a guide link. Use for 'what do I need "
            "for quest X' or 'which quests reward item Y' questions."
        ),
    )
    async def search_quests(
        query: Annotated[str, Field(description="Quest name search; empty lists all.")] = "",
        limit: Annotated[int, Field(ge=1, le=25)] = 10,
        page: Annotated[int, Field(ge=1)] = 1,
    ) -> dict[str, Any]:
        params: dict[str, Any] = {"limit": limit, "page": page}
        if query:
            params["search"] = query
        try:
            raw = await request_json(
                client,
                "GET",
                f"{metaforge}/quests",
                service="metaforge",
                params=params,
                unreachable_hint="MetaForge may be down; try again shortly.",
            )
        except ToolError as err:
            return err.payload()
        quests = [_project_quest(q) for q in (raw.get("data") or [])]
        pagination = raw.get("pagination") or {}
        return {
            "query": query,
            "quests": quests,
            "returned": len(quests),
            "total": pagination.get("total", len(quests)),
            "truncated": bool(pagination.get("hasNextPage")),
            "source": METAFORGE_SOURCE,
        }

    # ── trader stock ────────────────────────────────────────────────
    @mcp.tool(
        name="arc_get_trader_stock",
        description=(
            "Current ARC Raiders trader inventories: what each trader "
            "(Apollo, Celeste, Tian Wen, ...) sells right now and at what "
            "price. Optionally filter to one trader by name. Use for 'who "
            "sells X' or 'what does trader Y stock' questions. Cached up "
            "to 15 minutes."
        ),
    )
    async def get_trader_stock(
        trader: Annotated[str, Field(description="Trader name filter (case-insensitive).")] = "",
    ) -> dict[str, Any]:
        try:
            raw = await _cached_json(
                f"{metaforge}/traders",
                service="metaforge",
                ttl=TTL_LIVE,
                hint="MetaForge may be down; try again shortly.",
            )
        except ToolError as err:
            return err.payload()
        stock: dict[str, Any] = raw.get("data") or {}
        if trader:
            match = next((n for n in stock if n.lower() == trader.lower()), None)
            if match is None:
                return ToolError(
                    "metaforge_unknown_trader",
                    f"No trader named {trader!r}.",
                    f"Known traders: {', '.join(sorted(stock))}.",
                ).payload()
            stock = {match: stock[match]}
        traders = {
            name: [
                {
                    "id": i.get("id"),
                    "name": i.get("name"),
                    "type": i.get("item_type"),
                    "rarity": i.get("rarity"),
                    "price": i.get("trader_price"),
                }
                for i in items
            ]
            for name, items in stock.items()
        }
        return {"traders": traders, "source": METAFORGE_SOURCE}

    # ── event schedule ──────────────────────────────────────────────
    @mcp.tool(
        name="arc_get_event_schedule",
        description=(
            "The rotating ARC Raiders in-raid event schedule (Harvester, "
            "Night Raid, Matriarch, Hurricane, ...): which events run on "
            "which map, with UTC start/end times and an active/upcoming "
            "status. Optionally filter by map name. Use for 'what events "
            "are on right now / coming up' questions. Cached up to 15 "
            "minutes."
        ),
    )
    async def get_event_schedule(
        map_name: Annotated[str, Field(description="Map name filter (substring match).")] = "",
        include_past: Annotated[bool, Field(description="Also return ended events.")] = False,
    ) -> dict[str, Any]:
        try:
            raw = await _cached_json(
                f"{metaforge}/events-schedule",
                service="metaforge",
                ttl=TTL_LIVE,
                hint="MetaForge may be down; try again shortly.",
            )
        except ToolError as err:
            return err.payload()
        now_ms = time.time() * 1000
        events = []
        for ev in raw.get("data") or []:
            start, end = ev.get("startTime"), ev.get("endTime")
            if map_name and map_name.lower() not in (ev.get("map") or "").lower():
                continue
            if isinstance(end, (int, float)) and end <= now_ms:
                status = "ended"
                if not include_past:
                    continue
            elif isinstance(start, (int, float)) and start <= now_ms:
                status = "active"
            else:
                status = "upcoming"
            events.append(
                {
                    "name": ev.get("name"),
                    "map": ev.get("map"),
                    "status": status,
                    "starts_utc": _iso_utc(start),
                    "ends_utc": _iso_utc(end),
                }
            )
        return {"events": events, "returned": len(events), "source": METAFORGE_SOURCE}

    # ── maps ────────────────────────────────────────────────────────
    @mcp.tool(
        name="arc_list_maps",
        description=(
            "List the ARC Raiders playable maps (Dam Battlegrounds, Buried "
            "City, Spaceport, ...) with their canonical ids and map images. "
            "Use to resolve a map name before filtering events, or for "
            "'what maps exist' questions. Cached up to 6 hours."
        ),
    )
    async def list_maps() -> dict[str, Any]:
        try:
            raw = await _cached_json(
                f"{data_base}/maps.json",
                service="arcraiders_data",
                ttl=TTL_STATIC,
                hint="raw.githubusercontent.com may be unreachable; try again shortly.",
            )
        except ToolError as err:
            return err.payload()
        maps = [
            {
                "id": m.get("id"),
                "name": (m.get("name") or {}).get("en"),
                "image": m.get("image"),
            }
            for m in (raw if isinstance(raw, list) else [])
        ]
        return {"maps": maps, "returned": len(maps), "source": RAIDTHEORY_SOURCE}

    # ── wiki ────────────────────────────────────────────────────────
    @mcp.tool(
        name="arc_search_wiki",
        description=(
            "Full-text search of the ARC Raiders Wiki (the Embark-supported "
            "community wiki). Returns page titles and snippets. Use when the "
            "structured item/quest tools don't cover it — lore, mechanics, "
            "strategies, patch details — then fetch a hit with "
            "arc_get_wiki_page."
        ),
    )
    async def search_wiki(
        query: Annotated[str, Field(min_length=1, description="Free-text wiki search.")],
        limit: Annotated[int, Field(ge=1, le=20)] = 5,
    ) -> dict[str, Any]:
        try:
            raw = await request_json(
                client,
                "GET",
                wiki_api,
                service="arcraiders_wiki",
                params={
                    "action": "query",
                    "list": "search",
                    "srsearch": query,
                    "srlimit": limit,
                    "format": "json",
                },
                unreachable_hint="arcraiders.wiki may be down; try again shortly.",
            )
        except ToolError as err:
            return err.payload()
        info = (raw.get("query") or {}).get("searchinfo") or {}
        hits = [
            {
                "title": h.get("title"),
                "snippet": _strip_tags(h.get("snippet") or ""),
                "last_edited": h.get("timestamp"),
            }
            for h in (raw.get("query") or {}).get("search") or []
        ]
        return {
            "query": query,
            "results": hits,
            "returned": len(hits),
            "total": info.get("totalhits", len(hits)),
            "truncated": info.get("totalhits", 0) > len(hits),
            "source": WIKI_SOURCE,
        }

    @mcp.tool(
        name="arc_get_wiki_page",
        description=(
            "Fetch one ARC Raiders Wiki page by exact title: the prose as "
            "plain text PLUS the raw wikitext (whose infoboxes carry data "
            "the prose omits, e.g. per-tier weapon stats and durability). "
            "Titles come from arc_search_wiki. Content is CC BY-SA 4.0 — "
            "attribute the wiki when reusing it."
        ),
    )
    async def get_wiki_page(
        title: Annotated[str, Field(min_length=1, description="Exact wiki page title.")],
    ) -> dict[str, Any]:
        try:
            extract_raw = await request_json(
                client,
                "GET",
                wiki_api,
                service="arcraiders_wiki",
                params={
                    "action": "query",
                    "prop": "extracts",
                    "titles": title,
                    "explaintext": 1,
                    "redirects": 1,
                    "format": "json",
                },
                unreachable_hint="arcraiders.wiki may be down; try again shortly.",
            )
        except ToolError as err:
            return err.payload()
        pages = ((extract_raw.get("query") or {}).get("pages")) or {}
        page = next(iter(pages.values()), None)
        if page is None or "missing" in page:
            return ToolError(
                "arcraiders_wiki_page_not_found",
                f"No wiki page titled {title!r}.",
                "Titles are exact (though redirects are followed) — find one "
                "with arc_search_wiki first.",
            ).payload()

        wikitext = ""
        try:
            parse_raw = await request_json(
                client,
                "GET",
                wiki_api,
                service="arcraiders_wiki",
                params={
                    "action": "parse",
                    "page": page.get("title") or title,
                    "prop": "wikitext",
                    "redirects": 1,
                    "format": "json",
                },
            )
            wikitext = ((parse_raw.get("parse") or {}).get("wikitext") or {}).get("*", "")
        except ToolError:
            # The plaintext extract alone is still a useful answer — degrade
            # rather than fail the whole call on the second request.
            log.warning("wikitext fetch failed for %r; returning extract only", title)

        extract = page.get("extract") or ""
        return {
            "title": page.get("title"),
            "url": f"https://arcraiders.wiki/wiki/{(page.get('title') or title).replace(' ', '_')}",
            "text": extract[:WIKI_TEXT_MAX],
            "text_truncated": len(extract) > WIKI_TEXT_MAX,
            "wikitext": wikitext[:WIKI_TEXT_MAX],
            "wikitext_truncated": len(wikitext) > WIKI_TEXT_MAX,
            "license": "CC BY-SA 4.0",
            "source": WIKI_SOURCE,
        }
