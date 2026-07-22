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
from typing import TYPE_CHECKING, Annotated, Any, cast

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
_NORM_RE = re.compile(r"[^a-z0-9]+")

# Known hideout module files in RaidTheory/arcraiders-data. Used as a
# fallback when the GitHub directory-listing API is unavailable (e.g.
# unauthenticated rate limit) so arc_check_item_keep degrades to the
# known set instead of losing hideout data entirely.
FALLBACK_HIDEOUT_MODULES = [
    "equipment_bench.json",
    "explosives_bench.json",
    "med_station.json",
    "refiner.json",
    "scrappy.json",
    "stash.json",
    "utility_bench.json",
    "weapon_bench.json",
    "workbench.json",
]


def _strip_tags(fragment: str) -> str:
    """Drop HTML tags and unescape entities from a wiki search snippet."""
    return html.unescape(_TAG_RE.sub("", fragment))


def _norm(name: str) -> str:
    """Normalize an item name/slug for cross-dataset matching.

    RaidTheory uses snake_case ids ('arc_alloy'), MetaForge uses kebab ids
    ('metal-parts') and display names ('ARC Alloy'); all collapse to the
    same token string ('arc alloy') under this normalization.
    """
    return _NORM_RE.sub(" ", name.lower()).strip()


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


def _project_quest_items(raw: dict[str, Any], key: str) -> list[dict[str, Any]]:
    """Reduce a MetaForge quest item list (rewards / required_items) to names."""
    return [
        {
            "item": (r.get("item") or {}).get("name") or r.get("item_id"),
            "rarity": (r.get("item") or {}).get("rarity"),
            "quantity": r.get("quantity"),
        }
        for r in raw.get(key) or []
    ]


def _project_quest(raw: dict[str, Any]) -> dict[str, Any]:
    """Reduce a MetaForge quest to objectives, giver, turn-ins, and rewards."""
    guide = raw.get("guide_url")
    return {
        "id": raw.get("id"),
        "name": raw.get("name"),
        "trader": raw.get("trader_name"),
        "objectives": raw.get("objectives") or [],
        "xp": raw.get("xp"),
        "required_items": _project_quest_items(raw, "required_items"),
        "rewards": _project_quest_items(raw, "rewards"),
        "guide_url": f"https://metaforge.app{guide}" if guide else None,
    }


def register(mcp: FastMCP, settings: Settings) -> None:
    """Register arc_* ARC Raiders game-data tools on the given MCP server."""
    metaforge = settings.arcraiders_metaforge_base_url.rstrip("/")
    data_base = settings.arcraiders_data_base_url.rstrip("/")
    data_listing = settings.arcraiders_data_listing_url.rstrip("/")
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

    async def _all_quests() -> list[dict[str, Any]]:
        """Every quest, aggregated across pages and cached as one unit."""
        cache_key = f"{metaforge}/quests#all"
        cached = cache.get(cache_key)
        if cached is not None:
            return cast(list[dict[str, Any]], cached)
        quests: list[dict[str, Any]] = []
        page = 1
        while page <= 10:  # backstop: 1000 quests, far above the real count
            raw = await request_json(
                client,
                "GET",
                f"{metaforge}/quests",
                service="metaforge",
                params={"limit": 100, "page": page},
                unreachable_hint="MetaForge may be down; try again shortly.",
            )
            quests.extend(raw.get("data") or [])
            if not (raw.get("pagination") or {}).get("hasNextPage"):
                break
            page += 1
        cache.put(cache_key, quests, TTL_STATIC)
        return quests

    async def _hideout_modules() -> list[dict[str, Any]]:
        """All hideout module definitions, aggregated and cached as one unit.

        The file list comes from the GitHub contents API when reachable
        (so new modules appear without a code change) and falls back to
        the known set when it isn't (e.g. unauthenticated rate limit).
        """
        cache_key = f"{data_base}/hideout#all"
        cached = cache.get(cache_key)
        if cached is not None:
            return cast(list[dict[str, Any]], cached)
        try:
            listing = await request_json(
                client, "GET", f"{data_listing}/hideout", service="arcraiders_data"
            )
            files = [
                str(e["name"])
                for e in listing
                if isinstance(e, dict) and str(e.get("name", "")).endswith(".json")
            ]
        except ToolError:
            log.warning("hideout dir listing failed; using fallback module list")
            files = list(FALLBACK_HIDEOUT_MODULES)
        modules: list[dict[str, Any]] = []
        for fname in files:
            try:
                mod = await request_json(
                    client, "GET", f"{data_base}/hideout/{fname}", service="arcraiders_data"
                )
            except ToolError:
                log.warning("hideout module %s fetch failed; skipping", fname)
                continue
            modules.extend(mod if isinstance(mod, list) else [mod])
        if modules:
            # Don't cache an empty aggregate — a transient outage would
            # otherwise pin "no hideout data" for the whole TTL.
            cache.put(cache_key, modules, TTL_STATIC)
        return modules

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

    # ── keep-or-sell ────────────────────────────────────────────────
    @mcp.tool(
        name="arc_check_item_keep",
        description=(
            "Keep/sell/recycle helper — the one call for 'is this ARC "
            "Raiders item worth keeping?'. Resolves the item (value, "
            "rarity, weight), then cross-references every quest turn-in "
            "that requires it, every hideout module upgrade level that "
            "needs it (with quantities), and which traders sell it. An "
            "item needed for quests or hideout upgrades is usually worth "
            "keeping regardless of sell value. Crafting/recycling uses "
            "are not covered — check the item's wiki page via "
            "arc_get_wiki_page for those."
        ),
    )
    async def check_item_keep(
        item: Annotated[str, Field(min_length=1, description="Item name, e.g. 'ARC Alloy'.")],
    ) -> dict[str, Any]:
        # Resolve the item via MetaForge search; prefer an exact
        # normalized-name match over the first fuzzy hit.
        try:
            raw = await request_json(
                client,
                "GET",
                f"{metaforge}/items",
                service="metaforge",
                params={"search": item, "limit": 25},
                unreachable_hint="MetaForge may be down; try again shortly.",
            )
        except ToolError as err:
            return err.payload()
        candidates = raw.get("data") or []
        if not candidates:
            return ToolError(
                "metaforge_item_not_found",
                f"No item matching {item!r}.",
                "Check the spelling with arc_search_items.",
            ).payload()
        want = _norm(item)
        best = next((c for c in candidates if _norm(c.get("name") or "") == want), candidates[0])
        # Every identity this item goes by across the datasets.
        keys = {_norm(best.get("name") or ""), _norm(best.get("id") or "")} - {""}

        notes: list[str] = []

        # Quest turn-ins requiring it. Each cross-reference degrades to
        # None + a note on failure instead of failing the whole call.
        quests_requiring: list[dict[str, Any]] | None
        try:
            quests_requiring = [
                {
                    "quest": quest.get("name"),
                    "trader": quest.get("trader_name"),
                    "quantity": req.get("quantity"),
                }
                for quest in await _all_quests()
                for req in quest.get("required_items") or []
                if {
                    _norm(((req.get("item") or {}).get("name")) or ""),
                    _norm(req.get("item_id") or ""),
                }
                & keys
            ]
        except ToolError:
            quests_requiring = None
            notes.append("Quest data unavailable (MetaForge fetch failed).")

        # Hideout upgrade levels requiring it.
        hideout_requiring: list[dict[str, Any]] | None = None
        modules = await _hideout_modules()
        if modules:
            hideout_requiring = [
                {
                    "module": (mod.get("name") or {}).get("en") or mod.get("id"),
                    "level": lvl.get("level"),
                    "quantity": req.get("quantity"),
                }
                for mod in modules
                for lvl in mod.get("levels") or []
                for req in lvl.get("requirementItemIds") or []
                if _norm(str(req.get("itemId") or "")) in keys
            ]
        else:
            notes.append("Hideout data unavailable (RaidTheory fetch failed).")

        # Trader offers (who sells it, at what price).
        trader_offers: list[dict[str, Any]] | None
        try:
            traders_raw = await _cached_json(
                f"{metaforge}/traders", service="metaforge", ttl=TTL_LIVE
            )
            trader_offers = [
                {"trader": trader_name, "price": entry.get("trader_price")}
                for trader_name, stock in (traders_raw.get("data") or {}).items()
                for entry in stock or []
                if {_norm(entry.get("name") or ""), _norm(entry.get("id") or "")} & keys
            ]
        except ToolError:
            trader_offers = None
            notes.append("Trader data unavailable (MetaForge fetch failed).")

        return {
            "item": _project_item(best),
            "match": "exact" if _norm(best.get("name") or "") == want else "closest",
            "other_candidates": [c.get("name") for c in candidates[:6] if c is not best],
            "quests_requiring": quests_requiring,
            "hideout_requiring": hideout_requiring,
            "trader_offers": trader_offers,
            "notes": notes,
            "sources": [METAFORGE_SOURCE, RAIDTHEORY_SOURCE],
        }

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
