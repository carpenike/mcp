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

import contextlib
import html
import logging
import re
import time
from typing import TYPE_CHECKING, Annotated, Any, cast

from mcp.types import ToolAnnotations
from pydantic import Field

from homelab_mcp.arcraiders_state import RAID_OUTCOMES, ArcState
from homelab_mcp.tools._http import ToolError, enc, make_client, request_json

if TYPE_CHECKING:
    from mcp.server.fastmcp import FastMCP

    from homelab_mcp.config import Settings

log = logging.getLogger(__name__)

# Server-level guidance sent to MCP clients at connection time (see
# _registry.collect_instructions). Loaded into every client context —
# keep it tight and workflow-shaped, not a restatement of tool schemas.
INSTRUCTIONS = """\
## ARC Raiders tools (arc_*)

Game-data lookups for ARC Raiders (items, quests, traders, events, maps, wiki).

**Keep/sell/recycle questions** ("is X worth keeping?"): call
`arc_check_item_keep` first — it cross-references quest turn-ins, hideout
upgrades, expedition/seasonal project phases, recycle/salvage outputs, and
trader offers, then ships a `verdict` with `verdict_reason` and
`keep_quantity` (the summed demand — use it verbatim, never re-add the
arrays yourself). Honor `coverage`: axes marked not_modeled/unavailable
are invisible to the tool, and selling is irreversible — hedge before
advising a sale. If `match` is "closest", say what you matched and list
`other_candidates`. Check `variants` for damaged/intact siblings — which
form is required comes from data, not naming (Damaged Heat Sink IS the
required form).

**"What should I build next?"**: call `arc_plan_upgrades` with the user's
module levels (omit unknown ones — never guess), and their stash counts
when known. It returns per-module shortfalls, `nearest_completion`,
shared-item `contention`, and a deduped shopping list. Trust its
arithmetic over your own; `have`/`short` null means stash unknown, 0
means known-empty — don't conflate them.

**Time-sensitive data**: `arc_get_event_schedule` (in-raid event rotation)
and `arc_get_trader_stock` change hourly-ish; timestamps are UTC. Compare
against the current time and lead with what's active now or starting soon
("Matriarch on Blue Gate for another 40 min"). In long conversations,
re-call these tools rather than reusing results fetched earlier — a
schedule fetched an hour ago is wrong now. When the user says they're
about to raid a specific map, proactively check that map's events.

These tools are pull-only: you cannot watch the schedule in the
background or alert the user when an event starts. If the user asks for
that, say so and suggest they either ask again when they want a fresh
check, or set up a client-side scheduled task/automation that calls
these tools on an interval.

**Where-to-find questions**: `arc_check_item_keep` returns three levels —
`loot_area` (which loot pool drops it), `found_on_maps`, and
`wiki_location` (named-region prose like 'West of Olive Grove'; the
item's wiki page via `arc_get_wiki_page` often adds an in-game location
screenshot). For pixel-level markers, share the MapGenie links the tools
return: `map_links` open pre-filtered to the item (`?search=`), while
`map_url`/`mapgenie_url` open the plain map. These tools cannot serve
MapGenie's marker data directly — link, don't ingest.

**ARC-sourced items & combat**: when loot_area is just "ARC", the real
answer is an enemy to kill — `arc_who_drops(item)` names it, then
`arc_get_enemy` gives threat, weakness, and kill tactics. For "which
weapon vs ARC" loadout questions use `arc_compare_weapons` — its
`armor_penetration` field is the deciding stat (item search lacks it).

**Personal raid history**: when the user recaps a raid ("died at
Spaceport, lost the Ferro"), offer to log it via `arc_log_raid` — one
sentence is enough; don't interrogate for every field. Answer "does
this loadout work for me" / "where do I keep dying" from
`arc_raid_stats` (their real history), not general wisdom. Mislogged?
`arc_list_raids` then `arc_delete_raid`. For "did X get nerfed/buffed"
claims, check `arc_patch_diff` before hedging — if it shows no change
in the window, say so with the snapshot dates.

Data comes from community sources (MetaForge, RaidTheory, arcraiders.wiki,
ardb.app) and may lag the newest game patch; responses carry `source`
fields — keep that attribution when presenting data publicly. For lore
and strategies beyond the structured data, use `arc_search_wiki` then
`arc_get_wiki_page` (infobox stats live in the returned `wikitext`).
"""

METAFORGE_SOURCE = "MetaForge — https://metaforge.app/arc-raiders"
RAIDTHEORY_SOURCE = (
    "RaidTheory/arcraiders-data (MIT) — "
    "https://github.com/RaidTheory/arcraiders-data · https://arctracker.io"
)
WIKI_SOURCE = "ARC Raiders Wiki (CC BY-SA 4.0) — https://arcraiders.wiki"
ARDB_SOURCE = "ardb.app — https://ardb.app"

# Cache TTLs (seconds). Live data (trader stock, event rotation) turns over
# on the order of hours; map metadata changes only on game patches.
TTL_LIVE = 900
TTL_STATIC = 21600

# Wiki pages can be long; cap what we return so one tool call can't flood
# the context window. Truncation is always flagged, never silent.
WIKI_TEXT_MAX = 8000

_TAG_RE = re.compile(r"<[^>]+>")
_NORM_RE = re.compile(r"[^a-z0-9]+")

# Every arc_* tool is a pure lookup against public upstreams. Declaring
# that explicitly matters: claude.ai's permission UI groups a connector's
# tools by these hints ("Read-only tools" vs "Write/delete tools") and
# lumps unannotated tools into an individually-approved "Other tools"
# bucket. openWorldHint stays true — these DO reach external services.
READ_ONLY = ToolAnnotations(
    readOnlyHint=True,
    destructiveHint=False,
    idempotentHint=True,
    openWorldHint=True,
)

# Personal-state tools (raid log, patch diff) touch only the local SQLite
# store — closed world. The log writer is additive and each call records
# a distinct raid (not idempotent); the delete is the correction path and
# follows the repo's destructive+confirm-gate pattern.
READ_ONLY_LOCAL = ToolAnnotations(
    readOnlyHint=True,
    destructiveHint=False,
    idempotentHint=True,
    openWorldHint=False,
)
WRITE_LOG = ToolAnnotations(
    readOnlyHint=False,
    destructiveHint=False,
    idempotentHint=False,
    openWorldHint=False,
)
DESTRUCTIVE_LOCAL = ToolAnnotations(
    readOnlyHint=False,
    destructiveHint=True,
    idempotentHint=True,
    openWorldHint=False,
)

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

# Deep links into MapGenie's interactive maps (probed 2026-07: each slug
# returns 200; note 'spaceport' but 'the-blue-gate'). Linking OUT is fine —
# ingesting their marker data is prohibited by their ToS, so links are the
# only integration. Keyed by _norm()-ed map name to absorb the id/display
# variants across MetaForge ('dam', 'Blue Gate') and RaidTheory
# ('dam_battlegrounds').
_MAPGENIE_BASE = "https://mapgenie.io/arc-raiders/maps/"
MAPGENIE_SLUGS = {
    "dam": "dam-battlegrounds",
    "dam battlegrounds": "dam-battlegrounds",
    "spaceport": "spaceport",
    "the spaceport": "spaceport",
    "buried city": "buried-city",
    "blue gate": "the-blue-gate",
    "the blue gate": "the-blue-gate",
    "stella montis": "stella-montis",
    # bots.json splits Stella Montis into lower/upper — one MapGenie map.
    "stella montis lower": "stella-montis",
    "stella montis upper": "stella-montis",
    "riven tides": "riven-tides",
}


def _pretty_map(map_id: str) -> str:
    """Display name for a RaidTheory snake_case map id ('the_spaceport' -> 'The Spaceport')."""
    return _norm(map_id).title().replace("Arc", "ARC")


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


def _snake(name: str) -> str:
    """RaidTheory per-item file slug for a display name ('ARC Alloy' -> 'arc_alloy')."""
    return _norm(name).replace(" ", "_")


def _qty(value: Any) -> int:
    """Coerce a quantity that may arrive as int or numeric string; 0 if unparseable."""
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


# Worst-first tiebreak for nearest_completion sorting; unknown rarities
# sort as worst so they never make a module look closer than it is.
RARITY_ORDER = {"Common": 0, "Uncommon": 1, "Rare": 2, "Epic": 3, "Legendary": 4}

# Damaged/intact (and similar) sibling prefixes. The relation labels are
# (variant_with_prefix, variant_without): 'Damaged Wasp Driver' is the
# degraded form of 'Wasp Driver'; the reverse relation is 'intact'.
_VARIANT_PREFIXES = {
    "damaged": ("degraded", "intact"),
    "broken": ("degraded", "intact"),
    "advanced": ("advanced", "standard"),
}


def _mapgenie_url(map_name: str | None, search: str | None = None) -> str | None:
    """Interactive-map deep link for a map name/id, or None if unknown.

    With `search`, the link opens pre-filtered: MapGenie's map app reads
    a `?search=` query param into its initial filter state (verified in
    their map.js: `search: t.get("search")` applied on load alongside
    locationIds). Marker-id deep links (`?locationIds=`) exist but need
    MapGenie's proprietary ids, which we don't ingest.
    """
    if not map_name:
        return None
    slug = MAPGENIE_SLUGS.get(_norm(str(map_name)))
    if slug is None:
        return None
    url = f"{_MAPGENIE_BASE}{slug}"
    return f"{url}?search={enc(search)}" if search else url


def _wiki_section(extract: str, heading: str) -> str | None:
    """Pull one section's prose out of a plaintext wiki extract.

    Extracts render headings as '== Location =='. Image-caption artifacts
    ('In-Game location') survive into the plaintext and are dropped.
    """
    m = re.search(
        rf"==\s*{re.escape(heading)}\s*==\n(.*?)(?:\n==|\Z)", extract, re.DOTALL | re.IGNORECASE
    )
    if not m:
        return None
    lines = [ln.strip() for ln in m.group(1).splitlines()]
    lines = [ln for ln in lines if ln and ln.lower() != "in-game location"]
    return "; ".join(lines) or None


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
    # Coarse "where do I find it" signals: the loot pool/area type that
    # drops it, and any maps with known spawn locations.
    found_on_maps = sorted(
        {str(loc.get("map")) for loc in raw.get("locations") or [] if loc.get("map")}
    )
    # subcategory almost always duplicates item_type — only keep a real refinement.
    subcategory = raw.get("subcategory") or None
    if subcategory == raw.get("item_type"):
        subcategory = None
    # MetaForge emits value 0 for unknown (e.g. Tellurion, a 7000-coin Epic).
    # Never pass 0 through — it reads as "worthless, sell it". Callers fall
    # back to the RaidTheory per-item value, then to null + a note.
    value = raw.get("value") or None
    return {
        "id": raw.get("id"),
        "name": raw.get("name"),
        "type": raw.get("item_type"),
        "subcategory": subcategory,
        "rarity": raw.get("rarity"),
        "value": value,
        "description": raw.get("description"),
        "workbench": raw.get("workbench"),
        "loot_area": raw.get("loot_area"),
        "found_on_maps": found_on_maps,
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
    ardb = settings.arcraiders_ardb_base_url.rstrip("/")
    wiki_api = settings.arcraiders_wiki_api_url
    # One pooled client for the lifetime of the process (see _http.make_client).
    # Three upstream hosts, so no base_url; MetaForge can be slow on cold cache.
    client = make_client(timeout=20.0)
    cache = _TTLCache()
    # Personal-state store (raid log + snapshots). A failed open (e.g. dev
    # box without /var/lib/homelab-mcp) must not abort registration of the
    # whole category — the state-backed tools degrade to a config error.
    store: ArcState | None
    try:
        store = ArcState(settings.arcraiders_db_path)
    except Exception:
        log.exception("arcraiders state store unavailable — raid log/patch diff disabled")
        store = None

    def _store_error() -> dict[str, Any]:
        return ToolError(
            "arcraiders_state_unavailable",
            "The ARC Raiders state store could not be opened.",
            "Check HOMELAB_MCP_ARCRAIDERS_DB_PATH and directory permissions.",
        ).payload()

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

    async def _all_items() -> list[dict[str, Any]]:
        """Every item (projected), aggregated across pages and cached as one unit.

        MetaForge's server-side search does word matching, so 'lightbulb'
        finds nothing while the item is named 'Light Bulb'. Fuzzy
        resolution needs the full list; projected, ~600 items are small.
        """
        cache_key = f"{metaforge}/items#all"
        cached = cache.get(cache_key)
        if cached is not None:
            return cast(list[dict[str, Any]], cached)
        items: list[dict[str, Any]] = []
        page = 1
        while page <= 20:  # backstop: 2000 items, far above the real count
            raw = await request_json(
                client,
                "GET",
                f"{metaforge}/items",
                service="metaforge",
                params={"limit": 100, "page": page},
                unreachable_hint="MetaForge may be down; try again shortly.",
            )
            items.extend(_project_item(i) for i in raw.get("data") or [])
            if not (raw.get("pagination") or {}).get("hasNextPage"):
                break
            page += 1
        if items:
            # Don't cache an empty aggregate — a transient outage would
            # otherwise pin "no items" for the whole TTL.
            cache.put(cache_key, items, TTL_STATIC)
            if store is not None:
                # Opportunistic patch snapshot as a side effect of the
                # (at most once per TTL) fresh fetch. Hash-deduped and
                # age-gated in the store, so this is nearly always a
                # single SELECT; failures never break the fetch path.
                try:
                    await store.maybe_snapshot(
                        "items",
                        {
                            str(i.get("name")): {
                                "value": i.get("value"),
                                "rarity": i.get("rarity"),
                                "type": i.get("type"),
                                "stats": i.get("stats") or {},
                            }
                            for i in items
                            if i.get("name")
                        },
                    )
                except Exception:
                    log.exception("item snapshot failed — continuing without it")
        return items

    def _match_score(want: str, candidate_name: str) -> int | None:
        """Rank how well a candidate item name matches the query (lower = better).

        0: same normalized name          ('Light Bulb' vs 'light bulb')
        1: same with spaces squashed     ('Light Bulb' vs 'lightbulb')
        2: query contained in name       ('Extended Light Mag' vs 'light mag')
        3: squashed containment          ('Light Bulb' vs 'ightbul')
        None: no match.
        """
        want_norm = _norm(want)
        want_squash = want_norm.replace(" ", "")
        cand_norm = _norm(candidate_name)
        cand_squash = cand_norm.replace(" ", "")
        if not want_norm:
            return None
        if cand_norm == want_norm:
            return 0
        if cand_squash == want_squash:
            return 1
        if want_norm in cand_norm:
            return 2
        if want_squash in cand_squash:
            return 3
        return None

    async def _resolve_item(query: str) -> tuple[dict[str, Any] | None, str, list[str]]:
        """Find the best item for a free-text name, spacing-insensitively.

        Returns (item, match_kind, other_candidate_names). match_kind is
        'exact' when the name matches up to normalization/spacing, else
        'closest'. Raises ToolError if the item list is unavailable.
        """
        scored = []
        for it in await _all_items():
            score = _match_score(query, it.get("name") or "")
            if score is not None:
                scored.append((score, it.get("name") or "", it))
        if not scored:
            return None, "none", []
        scored.sort(key=lambda entry: (entry[0], len(entry[1]), entry[1]))
        best = scored[0]
        return (
            best[2],
            "exact" if best[0] <= 1 else "closest",
            [name for _, name, _ in scored[1:7]],
        )

    async def _all_projects() -> list[dict[str, Any]]:
        """Expedition/seasonal projects (Trophy Display, Expeditions, ...), cached.

        Feedback-driven: these were invisible to arc_check_item_keep, so an
        empty result read as "safe to sell" for items a live project wanted.
        """
        raw = await _cached_json(
            f"{data_base}/projects.json",
            service="arcraiders_data",
            ttl=TTL_STATIC,
            hint="raw.githubusercontent.com may be unreachable; try again shortly.",
        )
        return raw if isinstance(raw, list) else []

    async def _item_detail(*name_candidates: str | None) -> dict[str, Any] | None:
        """RaidTheory per-item file (recyclesInto/salvagesInto/value/tip), or None.

        File slugs are snake_case of the display name; MetaForge ids are
        kebab and occasionally suffixed ('wires-recipe'), so try each
        candidate. A miss is common and quiet — not every item has a file
        under the guessed slug (upstream even has typo'd slugs).
        """
        for cand in name_candidates:
            if not cand:
                continue
            url = f"{data_base}/items/{_snake(cand)}.json"
            cached = cache.get(url)
            if cached is not None:
                return cast(dict[str, Any], cached)
            try:
                raw = await request_json(client, "GET", url, service="arcraiders_data")
            except ToolError:
                continue
            detail = raw[0] if isinstance(raw, list) and raw else raw
            if isinstance(detail, dict):
                cache.put(url, detail, TTL_STATIC)
                return detail
        return None

    async def _all_bots() -> list[dict[str, Any]]:
        """ARC enemy bestiary (RaidTheory bots.json): threat, weakness, drops, maps."""
        raw = await _cached_json(
            f"{data_base}/bots.json",
            service="arcraiders_data",
            ttl=TTL_STATIC,
            hint="raw.githubusercontent.com may be unreachable; try again shortly.",
        )
        return raw if isinstance(raw, list) else []

    async def _ardb_index() -> dict[str, str]:
        """ardb.app id lookup: normalized name/id -> ardb item id, cached."""
        cache_key = f"{ardb}/items#index"
        cached = cache.get(cache_key)
        if cached is not None:
            return cast(dict[str, str], cached)
        raw = await request_json(
            client,
            "GET",
            f"{ardb}/items",
            service="ardb",
            unreachable_hint="ardb.app may be down; try again shortly.",
        )
        rows = raw if isinstance(raw, list) else []
        index: dict[str, str] = {}
        for row in rows:
            row_id = str(row.get("id") or "")
            if not row_id:
                continue
            index[_norm(row.get("name") or "")] = row_id
            index.setdefault(_norm(row_id), row_id)
        if index:
            cache.put(cache_key, index, TTL_STATIC)
        return index

    async def _ardb_detail(*name_candidates: str | None) -> dict[str, Any] | None:
        """Full ardb.app item record (weaponSpecs, usedInCraft, droppedBy), or None."""
        try:
            index = await _ardb_index()
        except ToolError:
            return None
        item_id = None
        for cand in name_candidates:
            if cand and _norm(cand) in index:
                item_id = index[_norm(cand)]
                break
        if item_id is None:
            # Squash fallback: 'Ferro I' listed as 'ferro', spacing variants.
            for cand in name_candidates:
                if not cand:
                    continue
                squash = _norm(cand).replace(" ", "")
                hits = {v for k, v in index.items() if k.replace(" ", "") == squash}
                if len(hits) == 1:
                    item_id = hits.pop()
                    break
        if item_id is None:
            return None
        url = f"{ardb}/items/{enc(item_id)}"
        cached = cache.get(url)
        if cached is not None:
            return cast(dict[str, Any], cached)
        try:
            detail = await request_json(client, "GET", url, service="ardb")
        except ToolError:
            return None
        if isinstance(detail, dict):
            cache.put(url, detail, TTL_STATIC)
            return detail
        return None

    def _weapon_specs(ardb_item: dict[str, Any] | None) -> dict[str, Any] | None:
        """Normalize ardb weaponSpecs — flat, consistent across tiers, with
        armor_penetration (the ARC-effectiveness stat MetaForge lacks)."""
        specs = (ardb_item or {}).get("weaponSpecs")
        if not isinstance(specs, dict):
            return None
        stats = specs.get("stats") or {}
        return {
            "armor_penetration": specs.get("armorPenetration"),
            "ammo": specs.get("ammoType"),
            "firing_mode": specs.get("firingMode"),
            "mag_size": specs.get("magSize"),
            "damage": stats.get("damage"),
            "range": stats.get("range"),
            "fire_rate": stats.get("fireRate"),
            "stability": stats.get("stability"),
            "agility": stats.get("agility"),
            "stealth": stats.get("stealth"),
        }

    def _project_demand(
        keys: set[str], projects: list[dict[str, Any]], now: float
    ) -> list[dict[str, Any]]:
        """Active-project phase requirements matching an item's identity keys."""
        rows: list[dict[str, Any]] = []
        for proj in projects:
            if proj.get("disabled"):
                continue
            end = proj.get("endDate")
            if isinstance(end, (int, float)) and end < now:
                continue
            proj_name = (proj.get("name") or {}).get("en") or proj.get("id")
            for phase in proj.get("phases") or []:
                for req in phase.get("requirementItemIds") or []:
                    if _norm(str(req.get("itemId") or "")) not in keys:
                        continue
                    rows.append(
                        {
                            "project": proj_name,
                            "phase": phase.get("phase"),
                            "quantity": req.get("quantity"),
                            "ends_utc": (
                                _iso_utc(end * 1000) if isinstance(end, (int, float)) else None
                            ),
                        }
                    )
        return rows

    def _hideout_demand(keys: set[str], modules: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """Hideout upgrade-level requirements matching an item's identity keys."""
        return [
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

    def _quest_demand(keys: set[str], quests: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """Quest turn-in requirements matching an item's identity keys."""
        rows: list[dict[str, Any]] = []
        for quest in quests:
            for req in quest.get("required_items") or []:
                req_item = req.get("item") or {}
                req_keys = {_norm(req_item.get("name") or ""), _norm(req.get("item_id") or "")}
                if req_keys & keys:
                    rows.append(
                        {
                            "quest": quest.get("name"),
                            "trader": quest.get("trader_name"),
                            "quantity": req.get("quantity"),
                        }
                    )
        return rows

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
        annotations=READ_ONLY,
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
        note = None
        if not items and page == 1:
            # MetaForge's search is word-based: 'lightbulb' misses 'Light
            # Bulb'. Fall back to spacing-insensitive local matching over
            # the cached full item list before reporting an empty result.
            try:
                best, _, other_names = await _resolve_item(query)
            except ToolError:
                best = None
            if best is not None:
                all_items = await _all_items()
                by_name = {i.get("name"): i for i in all_items}
                items = [best] + [by_name[n] for n in other_names[: limit - 1] if n in by_name]
                total = len(items)
                note = "Server search had no hits; matched item names locally instead."
        return {
            "query": query,
            "items": items,
            "returned": len(items),
            "total": total,
            "truncated": bool(pagination.get("hasNextPage")),
            **({"note": note} if note else {}),
            "source": METAFORGE_SOURCE,
        }

    # ── quests ──────────────────────────────────────────────────────
    @mcp.tool(
        annotations=READ_ONLY,
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
        annotations=READ_ONLY,
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
        annotations=READ_ONLY,
        name="arc_check_item_keep",
        description=(
            "Keep/sell/recycle helper — the one call for 'is this ARC "
            "Raiders item worth keeping?'. Resolves the item (value, "
            "rarity, weight), then cross-references every quest turn-in, "
            "hideout upgrade level, AND expedition/seasonal project phase "
            "that requires it, plus recycle/salvage outputs and trader "
            "offers — and ships a verdict (keep/recycle/sell) with the "
            "total keep_quantity. Check the `coverage` field before "
            "advising a sale: axes marked not_modeled are invisible to "
            "this tool, and selling is irreversible."
        ),
    )
    async def check_item_keep(
        item: Annotated[str, Field(min_length=1, description="Item name, e.g. 'ARC Alloy'.")],
    ) -> dict[str, Any]:
        # Resolve the item spacing-insensitively against the full cached
        # item list ('lightbulb' must find 'Light Bulb'), preferring an
        # exact normalized match over fuzzier containment hits.
        try:
            best, match_kind, other_candidates = await _resolve_item(item)
        except ToolError as err:
            return err.payload()
        if best is None:
            return ToolError(
                "metaforge_item_not_found",
                f"No item matching {item!r}.",
                "Check the spelling with arc_search_items.",
            ).payload()
        # Every identity this item goes by across the datasets.
        keys = {_norm(best.get("name") or ""), _norm(best.get("id") or "")} - {""}

        notes: list[str] = []
        coverage = {
            "quests": "complete",
            "hideout": "complete",
            "projects": "complete",
            "crafting_recipes": "not_modeled",
            "events": "not_modeled",
        }

        # Quest turn-ins requiring it. Each cross-reference degrades to
        # None + a note on failure instead of failing the whole call.
        quests_requiring: list[dict[str, Any]] | None
        all_quests: list[dict[str, Any]] = []
        try:
            all_quests = await _all_quests()
            quests_requiring = _quest_demand(keys, all_quests)
        except ToolError:
            quests_requiring = None
            coverage["quests"] = "unavailable"
            notes.append("Quest data unavailable (MetaForge fetch failed).")

        # Hideout upgrade levels requiring it.
        hideout_requiring: list[dict[str, Any]] | None = None
        modules = await _hideout_modules()
        if modules:
            hideout_requiring = _hideout_demand(keys, modules)
        else:
            coverage["hideout"] = "unavailable"
            notes.append("Hideout data unavailable (RaidTheory fetch failed).")

        # Expedition/seasonal project phases requiring it (Trophy Display,
        # Expeditions, Converging Paths, ...). Feedback-driven: an item a
        # live project wants must never come back looking safe to sell.
        projects_requiring: list[dict[str, Any]] | None
        all_projects: list[dict[str, Any]] = []
        now_s = time.time()
        try:
            all_projects = await _all_projects()
            projects_requiring = _project_demand(keys, all_projects, now_s)
        except ToolError:
            projects_requiring = None
            coverage["projects"] = "unavailable"
            notes.append("Project data unavailable (RaidTheory fetch failed).")

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

        # Named-region prose from the wiki's Location section, e.g.
        # 'West of Olive Grove'. Best-effort: many items have no wiki
        # page or no Location section — that's a plain None, not a note.
        wiki_location: str | None = None
        try:
            wiki_raw = await request_json(
                client,
                "GET",
                wiki_api,
                service="arcraiders_wiki",
                params={
                    "action": "query",
                    "prop": "extracts",
                    "titles": best.get("name") or item,
                    "explaintext": 1,
                    "redirects": 1,
                    "format": "json",
                },
            )
            wiki_page = next(
                iter(((wiki_raw.get("query") or {}).get("pages") or {}).values()), None
            )
            if wiki_page and "missing" not in wiki_page:
                wiki_location = _wiki_section(wiki_page.get("extract") or "", "Location")
        except ToolError:
            log.debug("wiki location lookup failed for %r", item)

        item_name = best.get("name") or item
        map_links = {
            m: url
            for m in best.get("found_on_maps") or []
            if (url := _mapgenie_url(m, search=item_name))
        }

        # RaidTheory per-item detail: recycle/salvage outputs, a value
        # fallback for MetaForge's value-0-means-unknown rows, and the
        # community tip line.
        detail = await _item_detail(best.get("name"), best.get("id"))
        if best.get("value") is None and detail and detail.get("value"):
            best["value"] = detail["value"]
            notes.append("Sell value taken from RaidTheory (MetaForge reports 0/unknown).")
        if detail and detail.get("tip"):
            best["tip"] = detail["tip"]

        # ardb.app record: weapon specs (incl. armor penetration), the
        # crafting-usage index, the item's own recipe, and enemy drops.
        ardb_item = await _ardb_detail(best.get("name"), best.get("id"))
        weapon_specs = _weapon_specs(ardb_item)
        crafting_uses: list[str] | None = None
        craft_recipe: dict[str, Any] | None = None
        dropped_by: list[str] | None = None
        if ardb_item is not None:
            coverage["crafting_recipes"] = "complete"
            crafting_uses = [
                str(u.get("name") or u.get("id")) for u in ardb_item.get("usedInCraft") or []
            ]
            recipe_raw = ardb_item.get("craftingRequirement")
            if isinstance(recipe_raw, dict):
                craft_recipe = {
                    "output_amount": recipe_raw.get("outputAmount"),
                    "inputs": [
                        {
                            "item": (r.get("item") or {}).get("name")
                            or (r.get("item") or {}).get("id"),
                            "quantity": r.get("amount") or r.get("quantity"),
                        }
                        for r in recipe_raw.get("requiredItems") or []
                    ],
                }
            dropped_by = [
                str(e.get("name") or e.get("id")) for e in ardb_item.get("droppedBy") or []
            ]
            if best.get("value") is None and ardb_item.get("value"):
                best["value"] = ardb_item["value"]
                notes.append("Sell value taken from ardb.app (other sources report 0/unknown).")
        else:
            coverage["crafting_recipes"] = "unavailable"
        if best.get("value") is None:
            notes.append("value_unknown: no source reports a sell value for this item.")

        async def _outputs(field: str) -> list[dict[str, Any]] | None:
            if detail is None:
                return None
            raw_map = detail.get(field)
            if not isinstance(raw_map, dict):
                return []
            out = []
            for out_id, qty in raw_map.items():
                out_item, _, _ = await _resolve_item(str(out_id))
                out.append(
                    {
                        "item": (out_item or {}).get("name") or str(out_id),
                        "quantity": qty,
                        "value_each": (out_item or {}).get("value"),
                    }
                )
            return out

        recycles_to = await _outputs("recyclesInto")
        salvages_to = await _outputs("salvagesInto")
        if recycles_to is None:
            notes.append("Recycle/salvage data unavailable (no RaidTheory item file found).")

        # recycle output value minus sell value: positive means recycling
        # beats selling on raw coins. None when any value is unknown.
        recycle_value_delta: int | None = None
        if recycles_to and best.get("value") is not None:
            values = [_qty(o.get("value_each")) * _qty(o.get("quantity")) for o in recycles_to]
            if all(o.get("value_each") is not None for o in recycles_to):
                recycle_value_delta = sum(values) - _qty(best.get("value"))

        # Sibling variants (Damaged X vs X, Advanced X, ...) with the flag
        # that matters: is THAT variant itself required by anything? The
        # data must answer this — heuristics get Damaged Heat Sink wrong
        # (the damaged form IS the Utility Station II requirement).
        def _required(variant_keys: set[str]) -> bool:
            return bool(
                _quest_demand(variant_keys, all_quests)
                or _hideout_demand(variant_keys, modules)
                or _project_demand(variant_keys, all_projects, now_s)
            )

        variants: list[dict[str, Any]] = []
        best_norm = _norm(item_name)
        try:
            everything = await _all_items()
        except ToolError:
            everything = []
        for other in everything:
            other_name = other.get("name") or ""
            other_norm = _norm(other_name)
            if other_norm == best_norm:
                continue
            relation = None
            for prefix, (with_rel, without_rel) in _VARIANT_PREFIXES.items():
                if other_norm == f"{prefix} {best_norm}":
                    relation = with_rel
                elif best_norm == f"{prefix} {other_norm}":
                    relation = without_rel
                if relation:
                    break
            if relation:
                other_keys = {other_norm, _norm(other.get("id") or "")} - {""}
                variants.append(
                    {
                        "name": other_name,
                        "relation": relation,
                        "required": _required(other_keys),
                    }
                )

        # Verdict. keep_quantity sums every known demand so the caller
        # never re-does the arithmetic (the reported field failure: "farm
        # 23 Tick Pods" vs an actual requirement of 8).
        demand_bits: list[str] = []
        keep_quantity = 0
        for row in hideout_requiring or []:
            keep_quantity += _qty(row.get("quantity"))
            demand_bits.append(f"{row['module']} L{row['level']} ({_qty(row.get('quantity'))})")
        for row in quests_requiring or []:
            keep_quantity += _qty(row.get("quantity"))
            demand_bits.append(f"quest {row['quest']} ({_qty(row.get('quantity'))})")
        for row in projects_requiring or []:
            keep_quantity += _qty(row.get("quantity"))
            demand_bits.append(
                f"project {row['project']} phase {row['phase']} ({_qty(row.get('quantity'))})"
            )

        if keep_quantity > 0:
            verdict = "keep"
            shown = demand_bits[:4]
            more = len(demand_bits) - len(shown)
            verdict_reason = (
                "Required for " + "; ".join(shown) + (f"; +{more} more" if more > 0 else "")
            )
        elif recycles_to and any(_required({_norm(o["item"])}) for o in recycles_to):
            verdict = "recycle"
            needed = [o["item"] for o in recycles_to if _required({_norm(o["item"])})]
            verdict_reason = f"Recycles into {', '.join(needed)}, which upgrades/quests need"
        elif recycle_value_delta is not None and recycle_value_delta > 0:
            verdict = "recycle"
            verdict_reason = f"Recycle output is worth {recycle_value_delta} more than selling"
        else:
            unavailable = [k for k, v in coverage.items() if v == "unavailable"]
            verdict = "sell"
            verdict_reason = (
                "No known quest/hideout/project demand"
                + (
                    f" (caveat: {', '.join(unavailable)} data unavailable this call)"
                    if unavailable
                    else ""
                )
                + ("; sell value unknown" if best.get("value") is None else "")
            )
        if verdict != "keep" and crafting_uses:
            shown_uses = ", ".join(crafting_uses[:3])
            verdict_reason += f"; note: used to craft {shown_uses}" + (
                f" +{len(crafting_uses) - 3} more" if len(crafting_uses) > 3 else ""
            )

        sources = [METAFORGE_SOURCE, RAIDTHEORY_SOURCE]
        if ardb_item is not None:
            sources.append(ARDB_SOURCE)
        return {
            "item": best,
            "match": match_kind,
            "other_candidates": other_candidates,
            "verdict": verdict,
            "verdict_reason": verdict_reason,
            "keep_quantity": keep_quantity,
            "variants": variants,
            "weapon_specs": weapon_specs,
            "crafting_uses": crafting_uses,
            "craft_recipe": craft_recipe,
            "dropped_by": dropped_by,
            "recycles_to": recycles_to,
            "salvages_to": salvages_to,
            "recycle_value_delta": recycle_value_delta,
            "wiki_location": wiki_location,
            "map_links": map_links,
            "quests_requiring": quests_requiring,
            "hideout_requiring": hideout_requiring,
            "projects_requiring": projects_requiring,
            "trader_offers": trader_offers,
            "coverage": coverage,
            "notes": notes,
            "sources": sources,
        }

    # ── upgrade planner ─────────────────────────────────────────────
    @mcp.tool(
        annotations=READ_ONLY,
        name="arc_plan_upgrades",
        description=(
            "Hideout upgrade planner — answers 'what should I build next' "
            "and 'what am I N items away from' deterministically. Pass your "
            "current module levels (0 = not built; OMIT unknown modules — "
            "they are flagged, never assumed); optionally your stash counts "
            "(then have/short are computed — the stash is treated as a "
            "SHARED pool: per-module shortfalls do NOT allocate it, and the "
            "`contention` array flags items multiple upgrades are counting "
            "on; pass `priority` to greedily allocate in that order), "
            "target levels for multi-level jumps (cumulative bill with "
            "per-level breakdown), and include_quests=true to fold quest "
            "turn-ins into the totals. Do the math from this tool's output "
            "— never sum requirements by hand."
        ),
    )
    async def plan_upgrades(
        current_levels: Annotated[
            dict[str, int],
            Field(description="Module name -> current level. 0 = not built. Omit if unknown."),
        ],
        stash: Annotated[
            dict[str, int] | None,
            Field(description="Item name -> count on hand. Omit entirely if unknown."),
        ] = None,
        targets: Annotated[
            dict[str, int] | None,
            Field(description="Module name -> target level. Default: next level for each."),
        ] = None,
        include_quests: Annotated[
            bool, Field(description="Also fold quest turn-in demand into totals.")
        ] = False,
        priority: Annotated[
            list[str] | None,
            Field(description="Module order for greedy stash allocation. Default: no allocation."),
        ] = None,
    ) -> dict[str, Any]:
        modules_db = await _hideout_modules()
        if not modules_db:
            return ToolError(
                "arcraiders_data_unreachable",
                "Hideout module data is unavailable.",
                "raw.githubusercontent.com may be unreachable; try again shortly.",
            ).payload()

        by_key: dict[str, dict[str, Any]] = {}
        for mod in modules_db:
            en = (mod.get("name") or {}).get("en") or str(mod.get("id"))
            by_key[_norm(en)] = mod
            by_key[_norm(str(mod.get("id") or ""))] = mod
        valid_names = sorted(
            {(m.get("name") or {}).get("en") or str(m.get("id")) for m in modules_db}
        )

        def _module_for(key: str) -> dict[str, Any] | None:
            return by_key.get(_norm(key))

        for source in (current_levels, targets or {}, {p: 0 for p in priority or []}):
            for key in source:
                if _module_for(key) is None:
                    return ToolError(
                        "arcraiders_unknown_module",
                        f"Unknown hideout module {key!r}.",
                        f"Valid modules: {', '.join(valid_names)}.",
                    ).payload()

        try:
            all_items = await _all_items()
        except ToolError:
            all_items = []
        items_by_norm = {_norm(i.get("name") or ""): i for i in all_items}
        for i in all_items:
            items_by_norm.setdefault(_norm(i.get("id") or ""), i)

        def _display(item_id: str) -> tuple[str, dict[str, Any]]:
            info = items_by_norm.get(_norm(item_id)) or {}
            return info.get("name") or item_id, info

        # Stash resolution: exact normalized match wins; a unique squash
        # match resolves; anything ambiguous or unmatched is RETURNED,
        # never dropped (a silently dropped key inflates `short`) and
        # never guessed ('Wasp Driver' vs 'Damaged Wasp Driver').
        pool: dict[str, int] = {}
        unresolved_stash_keys: list[dict[str, Any]] = []
        for raw_key, count in (stash or {}).items():
            key_norm = _norm(raw_key)
            if key_norm in items_by_norm:
                pool[_norm(items_by_norm[key_norm].get("name") or raw_key)] = pool.get(
                    _norm(items_by_norm[key_norm].get("name") or raw_key), 0
                ) + _qty(count)
                continue
            squash = key_norm.replace(" ", "")
            matches = sorted(
                {
                    str(i.get("name"))
                    for n, i in items_by_norm.items()
                    if i.get("name")
                    and (
                        n.replace(" ", "") == squash
                        or (len(squash) > 3 and n.replace(" ", "") == squash.rstrip("s"))
                    )
                }
            )
            if len(matches) == 1 and matches[0]:
                pool[_norm(matches[0])] = pool.get(_norm(matches[0]), 0) + _qty(count)
            else:
                unresolved_stash_keys.append({"key": raw_key, "candidates": matches[:6]})
        have_stash = stash is not None

        # Per-module plans.
        planned: list[dict[str, Any]] = []
        demand_by_item: dict[str, dict[str, Any]] = {}

        def _add_demand(item_norm: str, display: str, qty: int, consumer: str) -> None:
            row = demand_by_item.setdefault(
                item_norm, {"item": display, "total_demand": 0, "demanded_by": []}
            )
            row["total_demand"] += qty
            row["demanded_by"].append(consumer)

        for key, cur_raw in current_levels.items():
            plan_mod = _module_for(key)
            if plan_mod is None:  # unreachable — validated above; satisfies mypy
                continue
            en = (plan_mod.get("name") or {}).get("en") or str(plan_mod.get("id"))
            max_level = _qty(plan_mod.get("maxLevel")) or max(
                (_qty(lv.get("level")) for lv in plan_mod.get("levels") or []), default=0
            )
            cur = _qty(cur_raw)
            target = _qty((targets or {}).get(key, min(cur + 1, max_level)))
            if target < cur:
                return ToolError(
                    "arcraiders_invalid_target",
                    f"{en}: target level {target} is below current level {cur}.",
                    "Downgrades aren't a thing.",
                ).payload()
            plan: dict[str, Any] = {
                "module": en,
                "from": cur,
                "to": min(target, max_level),
                "action": "upgrade" if cur > 0 else "build",
                "level_known": True,
            }
            if target > max_level:
                plan["note"] = f"target clamped to max level {max_level}"
            if cur >= max_level:
                plan.update(
                    {
                        "action": "complete",
                        "requirements": [],
                        "per_level": [],
                        "units_outstanding": 0,
                        "items_outstanding": 0,
                    }
                )
                planned.append(plan)
                continue

            need_by_item: dict[str, int] = {}
            per_level: list[dict[str, Any]] = []
            for lvl in plan_mod.get("levels") or []:
                lnum = _qty(lvl.get("level"))
                if not (cur < lnum <= min(target, max_level)):
                    continue
                lvl_rows = []
                for req in lvl.get("requirementItemIds") or []:
                    item_id = str(req.get("itemId") or "")
                    qty = _qty(req.get("quantity"))
                    display, _info = _display(item_id)
                    need_by_item[_norm(display)] = need_by_item.get(_norm(display), 0) + qty
                    lvl_rows.append({"item": display, "quantity": qty})
                    _add_demand(_norm(display), display, qty, f"{en} L{lnum}")
                per_level.append({"level": lnum, "requirements": lvl_rows})

            rows: list[dict[str, Any]] = []
            for item_norm, need in need_by_item.items():
                display, info = _display(item_norm)
                rows.append(
                    {
                        "item": info.get("name") or display,
                        "need": need,
                        "pool": pool.get(item_norm, 0) if have_stash else None,
                        "short": max(0, need - pool.get(item_norm, 0)) if have_stash else None,
                        "rarity": info.get("rarity"),
                    }
                )
            plan["requirements"] = rows
            plan["per_level"] = per_level
            plan["units_outstanding"] = sum(
                _qty(r["short"] if have_stash else r["need"]) for r in rows
            )
            plan["items_outstanding"] = sum(
                1 for r in rows if _qty(r["short"] if have_stash else r["need"]) > 0
            )
            planned.append(plan)

        # Optional quest demand fold-in (affects totals, not per-module plans).
        if include_quests:
            try:
                for quest in await _all_quests():
                    for req in quest.get("required_items") or []:
                        req_item = req.get("item") or {}
                        display = req_item.get("name") or str(req.get("item_id"))
                        _add_demand(
                            _norm(display),
                            display,
                            _qty(req.get("quantity")),
                            f"quest {quest.get('name')}",
                        )
            except ToolError:
                pass

        # Greedy allocation only on explicit priority (per-module `short`
        # then reflects what's left after higher-priority modules take
        # theirs). Default: no allocation — contention flags the overlap.
        if priority and have_stash:
            remaining = dict(pool)
            order = {
                _norm((_module_for(p) or {}).get("name", {}).get("en") or p): i
                for i, p in enumerate(priority)
            }
            for plan in sorted(
                planned,
                key=lambda pl: order.get(_norm(str(pl["module"])), len(order)),
            ):
                for row in plan.get("requirements") or []:
                    item_norm = _norm(str(row["item"]))
                    take = min(remaining.get(item_norm, 0), _qty(row["need"]))
                    remaining[item_norm] = remaining.get(item_norm, 0) - take
                    row["short"] = max(0, _qty(row["need"]) - take)
                plan["units_outstanding"] = sum(
                    _qty(r["short"]) for r in plan.get("requirements") or []
                )
                plan["items_outstanding"] = sum(
                    1 for r in plan.get("requirements") or [] if _qty(r["short"]) > 0
                )

        # Contention: items multiple consumers are counting on, or where
        # total demand exceeds the shared pool.
        contention = []
        for item_norm, row in demand_by_item.items():
            item_pool = pool.get(item_norm, 0) if have_stash else None
            over_pool = have_stash and row["total_demand"] > (item_pool or 0)
            shared = len(row["demanded_by"]) >= 2
            if over_pool or (shared and (not have_stash or over_pool)):
                contention.append(
                    {
                        "item": row["item"],
                        "total_demand": row["total_demand"],
                        "pool": item_pool,
                        "deficit": (
                            max(0, row["total_demand"] - (item_pool or 0)) if have_stash else None
                        ),
                        "demanded_by": row["demanded_by"],
                    }
                )
            elif shared and not have_stash:
                contention.append(
                    {
                        "item": row["item"],
                        "total_demand": row["total_demand"],
                        "pool": None,
                        "deficit": None,
                        "demanded_by": row["demanded_by"],
                    }
                )
        contention.sort(key=lambda c: -_qty(c["total_demand"]))

        # Deduped shopping list with acquisition signals.
        traders_by_item: dict[str, list[str]] = {}
        try:
            traders_raw = await _cached_json(
                f"{metaforge}/traders", service="metaforge", ttl=TTL_LIVE
            )
            for trader_name, stock_rows in (traders_raw.get("data") or {}).items():
                for entry in stock_rows or []:
                    traders_by_item.setdefault(_norm(entry.get("name") or ""), []).append(
                        trader_name
                    )
        except ToolError:
            pass
        shopping_list = []
        for item_norm, row in demand_by_item.items():
            info = items_by_norm.get(item_norm) or {}
            item_pool = pool.get(item_norm, 0) if have_stash else None
            short = max(0, row["total_demand"] - (item_pool or 0)) if have_stash else None
            if have_stash and short == 0:
                continue
            shopping_list.append(
                {
                    "item": row["item"],
                    "total_need": row["total_demand"],
                    "pool": item_pool,
                    "short": short,
                    "rarity": info.get("rarity"),
                    "loot_area": info.get("loot_area"),
                    "traders_selling": sorted(set(traders_by_item.get(item_norm, []))),
                }
            )
        shopping_list.sort(key=lambda s: -_qty(s["total_need"]))

        # Nearest completion: fewest outstanding units, then fewest distinct
        # items, then best worst-rarity (a Legendary short is further from
        # done than three Commons).
        def _worst_rarity(plan: dict[str, Any]) -> int:
            ranks = [
                RARITY_ORDER.get(str(r.get("rarity")), len(RARITY_ORDER))
                for r in plan.get("requirements") or []
                if _qty(r["short"] if have_stash else r["need"]) > 0
            ]
            return max(ranks, default=-1)

        incomplete = [p for p in planned if p["action"] != "complete"]
        nearest_completion = [
            p["module"]
            for p in sorted(
                incomplete,
                key=lambda p: (p["units_outstanding"], p["items_outstanding"], _worst_rarity(p)),
            )
        ]

        return {
            "modules": planned,
            "nearest_completion": nearest_completion,
            "contention": contention,
            "shopping_list": shopping_list,
            "unresolved_stash_keys": unresolved_stash_keys,
            "modules_with_unknown_level": sorted(
                name
                for name in valid_names
                if all(_module_for(k) is not by_key.get(_norm(name)) for k in current_levels)
            ),
            "coverage": {
                "hideout": "complete",
                "quests": "complete" if include_quests else "not_included",
                "projects": "not_modeled",
                "events": "not_modeled",
            },
            "sources": [RAIDTHEORY_SOURCE, METAFORGE_SOURCE],
        }

    # ── bestiary ────────────────────────────────────────────────────
    @mcp.tool(
        annotations=READ_ONLY,
        name="arc_get_enemy",
        description=(
            "ARC enemy bestiary: threat rating, weakness/kill tactics, "
            "which maps it appears on, what it drops, and destroy/loot XP. "
            "Use for 'how do I kill a Bastion' or 'what does a Wasp drop' "
            "questions. Enemy names: Bastion, Bombardier, Fireball, "
            "Hornet, Leaper, Matriarch, Pop, Rocketeer, Sentinel, "
            "Shredder, Snitch, Spotter, Tick, Wasp, ..."
        ),
    )
    async def get_enemy(
        name: Annotated[str, Field(min_length=1, description="Enemy name, e.g. 'Bastion'.")],
    ) -> dict[str, Any]:
        try:
            bots = await _all_bots()
        except ToolError as err:
            return err.payload()

        def _bot_display(b: dict[str, Any]) -> str:
            raw_name = b.get("name")
            en = raw_name.get("en") if isinstance(raw_name, dict) else raw_name
            return str(en or b.get("id") or "")

        want = _norm(name)
        bot = None
        for b in bots:
            bot_norms = {_norm(_bot_display(b)), _norm(str(b.get("id") or ""))}
            # ids are prefixed ('arc_bastion'); accept the bare form too.
            bot_norms |= {n.removeprefix("arc ") for n in bot_norms}
            if want in bot_norms:
                bot = b
                break
        if bot is None:
            known = sorted(_bot_display(b) for b in bots)
            return ToolError(
                "arcraiders_unknown_enemy",
                f"No ARC enemy matching {name!r}.",
                f"Known enemies: {', '.join(known)}.",
            ).payload()
        display = _bot_display(bot)
        try:
            items = await _all_items()
        except ToolError:
            items = []
        names_by_norm = {_norm(i.get("id") or ""): i.get("name") for i in items}
        names_by_norm.update({_norm(i.get("name") or ""): i.get("name") for i in items})
        maps = [_pretty_map(str(m)) for m in bot.get("maps") or []]
        return {
            "name": display,
            "type": bot.get("type"),
            "threat": bot.get("threat"),
            "weakness": bot.get("weakness"),
            "description": (
                (bot.get("description") or {}).get("en")
                if isinstance(bot.get("description"), dict)
                else bot.get("description")
            ),
            "maps": maps,
            "map_links": {m: url for m in maps if (url := _mapgenie_url(m))},
            "drops": [
                names_by_norm.get(_norm(str(d))) or _pretty_map(str(d))
                for d in bot.get("drops") or []
            ],
            "destroy_xp": bot.get("destroyXp"),
            "loot_xp": bot.get("lootXp"),
            "image": bot.get("image"),
            "source": RAIDTHEORY_SOURCE,
        }

    @mcp.tool(
        annotations=READ_ONLY,
        name="arc_who_drops",
        description=(
            "Inverse drop index: which ARC enemies drop a given item, with "
            "threat ratings and maps. The actionable answer to 'where do I "
            "get Wasp Drivers' when the loot_area is just 'ARC' — the "
            "answer is an enemy to kill, not a zone. Follow up with "
            "arc_get_enemy for kill tactics."
        ),
    )
    async def who_drops(
        item: Annotated[str, Field(min_length=1, description="Item name, e.g. 'ARC Alloy'.")],
    ) -> dict[str, Any]:
        try:
            best, match_kind, _others = await _resolve_item(item)
        except ToolError as err:
            return err.payload()
        target_keys = {_norm(item)}
        display = item
        if best is not None:
            display = best.get("name") or item
            target_keys |= {_norm(best.get("name") or ""), _norm(best.get("id") or "")}
        target_keys -= {""}
        try:
            bots = await _all_bots()
        except ToolError as err:
            return err.payload()
        droppers = []
        for bot in bots:
            drop_norms = {_norm(str(d)) for d in bot.get("drops") or []}
            if drop_norms & target_keys:
                bot_name = (
                    (bot.get("name") or {}).get("en")
                    if isinstance(bot.get("name"), dict)
                    else str(bot.get("name"))
                ) or str(bot.get("id"))
                droppers.append(
                    {
                        "enemy": bot_name,
                        "threat": bot.get("threat"),
                        "maps": [_pretty_map(str(m)) for m in bot.get("maps") or []],
                    }
                )
        # Cross-check ardb's droppedBy for enemies bots.json misses.
        ardb_item = await _ardb_detail(display, (best or {}).get("id"))
        seen = {_norm(str(d["enemy"])) for d in droppers}
        for extra in (ardb_item or {}).get("droppedBy") or []:
            extra_name = str(extra.get("name") or extra.get("id") or "")
            if extra_name and _norm(extra_name) not in seen:
                droppers.append({"enemy": extra_name, "threat": None, "maps": []})
        return {
            "item": display,
            "match": match_kind if best is not None else "unresolved",
            "dropped_by": droppers,
            "returned": len(droppers),
            "sources": [RAIDTHEORY_SOURCE, ARDB_SOURCE],
        }

    # ── weapon comparison ───────────────────────────────────────────
    @mcp.tool(
        annotations=READ_ONLY,
        name="arc_compare_weapons",
        description=(
            "Side-by-side weapon comparison with normalized stats INCLUDING "
            "armor_penetration — the stat that decides ARC-vs-player "
            "effectiveness and that item search lacks. Pass 2-6 weapon "
            "names (tiers are distinct: 'Ferro I' vs 'Ferro IV'). Use for "
            "'which weapon is better vs ARC' loadout questions."
        ),
    )
    async def compare_weapons(
        weapons: Annotated[
            list[str],
            Field(min_length=2, max_length=6, description="Weapon names to compare."),
        ],
    ) -> dict[str, Any]:
        rows = []
        notes: list[str] = []
        for w in weapons:
            ardb_item = await _ardb_detail(w)
            specs = _weapon_specs(ardb_item)
            if ardb_item is None:
                notes.append(f"{w!r}: not found in ardb.app.")
                continue
            if specs is None:
                notes.append(f"{ardb_item.get('name') or w!r} has no weapon specs (not a weapon?).")
                continue
            rows.append(
                {
                    "name": ardb_item.get("name") or w,
                    "rarity": ardb_item.get("rarity"),
                    "value": ardb_item.get("value"),
                    "weight": ardb_item.get("weight"),
                    **specs,
                }
            )
        return {
            "weapons": rows,
            "returned": len(rows),
            "notes": notes,
            "source": ARDB_SOURCE,
        }

    # ── raid log ────────────────────────────────────────────────────
    @mcp.tool(
        annotations=WRITE_LOG,
        name="arc_log_raid",
        description=(
            "Append one raid to the personal raid log: map, outcome "
            "(extracted/died/disconnected), and optionally loadout, "
            "intent, where you died, approximate loot value, and notes. "
            "Quick capture is the point — log from one sentence of "
            "post-raid recap. Feeds arc_raid_stats."
        ),
    )
    async def log_raid(
        map_name: Annotated[str, Field(min_length=1, description="Map, e.g. 'Dam'.")],
        outcome: Annotated[str, Field(description="One of: extracted, died, disconnected.")],
        loadout: Annotated[
            str, Field(description="Weapon/shield summary, e.g. 'Ferro IV + medium shield'.")
        ] = "",
        intent: Annotated[str, Field(description="What the run was for.")] = "",
        died_at: Annotated[str, Field(description="POI/location of death, if died.")] = "",
        loot_value: Annotated[
            int | None, Field(ge=0, description="Approximate extracted loot value in coins.")
        ] = None,
        notes: Annotated[str, Field(description="Anything worth remembering.")] = "",
    ) -> dict[str, Any]:
        if store is None:
            return _store_error()
        if outcome not in RAID_OUTCOMES:
            return ToolError(
                "arcraiders_invalid_outcome",
                f"Unknown outcome {outcome!r}.",
                f"Valid outcomes: {', '.join(RAID_OUTCOMES)}.",
            ).payload()
        # Canonicalize the map name via the slug table ('dam' and 'Dam
        # Battlegrounds' must aggregate together in stats), but never
        # reject — new maps ship before our alias table learns them.
        norm = _norm(map_name)
        pretty = _pretty_map(MAPGENIE_SLUGS[norm]) if norm in MAPGENIE_SLUGS else map_name
        raid_id = await store.log_raid(
            map_name=pretty,
            outcome=outcome,
            loadout=loadout or None,
            intent=intent or None,
            died_at=died_at or None,
            loot_value=loot_value,
            notes=notes or None,
        )
        return {"logged": True, "id": raid_id, "map": pretty, "outcome": outcome}

    @mcp.tool(
        annotations=READ_ONLY_LOCAL,
        name="arc_list_raids",
        description=(
            "Recent raids from the personal raid log, newest first, "
            "optionally filtered by map. Use to review history or find a "
            "raid id for arc_delete_raid."
        ),
    )
    async def list_raids(
        limit: Annotated[int, Field(ge=1, le=100)] = 20,
        map_name: Annotated[str, Field(description="Exact map filter.")] = "",
    ) -> dict[str, Any]:
        if store is None:
            return _store_error()
        rows = await store.list_raids(limit=limit, map_name=map_name or None)
        for row in rows:
            row["ts_utc"] = _iso_utc(row.pop("ts") * 1000)
        return {"raids": rows, "returned": len(rows)}

    @mcp.tool(
        annotations=DESTRUCTIVE_LOCAL,
        name="arc_delete_raid",
        description=(
            "Delete one mislogged raid by id (from arc_list_raids). "
            "Without confirm=true this returns a non-destructive preview "
            "of the raid that WOULD be deleted."
        ),
    )
    async def delete_raid(
        raid_id: Annotated[int, Field(ge=1, description="Raid id to delete.")],
        confirm: Annotated[bool, Field(description="Actually delete.")] = False,
    ) -> dict[str, Any]:
        if store is None:
            return _store_error()
        row = await store.get_raid(raid_id)
        if row is None:
            return ToolError(
                "arcraiders_raid_not_found",
                f"No raid with id {raid_id}.",
                "List raids with arc_list_raids.",
            ).payload()
        row["ts_utc"] = _iso_utc(row.pop("ts") * 1000)
        if not confirm:
            return {"deleted": False, "preview": row, "hint": "Pass confirm=true to delete."}
        await store.delete_raid(raid_id)
        return {"deleted": True, "raid": row}

    @mcp.tool(
        annotations=READ_ONLY_LOCAL,
        name="arc_raid_stats",
        description=(
            "Personal raid analytics over the log: extraction rate "
            "overall, per map, and per loadout; where you die most; loot "
            "totals. Answers 'does Ferro actually extract more often' and "
            "'where do I keep dying' from YOUR history, not wikis."
        ),
    )
    async def raid_stats(
        days: Annotated[int, Field(ge=1, le=365, description="Lookback window.")] = 30,
    ) -> dict[str, Any]:
        if store is None:
            return _store_error()
        rows = await store.raid_rows_since(time.time() - days * 86400)
        if not rows:
            return {"days": days, "raids": 0, "hint": "Nothing logged yet — arc_log_raid."}

        def _rate(subset: list[dict[str, Any]]) -> dict[str, Any]:
            extracted = sum(1 for r in subset if r["outcome"] == "extracted")
            return {
                "raids": len(subset),
                "extracted": extracted,
                "extraction_rate": round(extracted / len(subset), 2),
            }

        def _grouped(key: str) -> dict[str, dict[str, Any]]:
            groups: dict[str, list[dict[str, Any]]] = {}
            for r in rows:
                if r.get(key):
                    groups.setdefault(str(r[key]), []).append(r)
            return {
                name: _rate(subset)
                for name, subset in sorted(groups.items(), key=lambda kv: -len(kv[1]))
            }

        deaths: dict[str, int] = {}
        for r in rows:
            if r["outcome"] == "died" and r.get("died_at"):
                deaths[str(r["died_at"])] = deaths.get(str(r["died_at"]), 0) + 1
        loot = [r["loot_value"] for r in rows if r.get("loot_value") is not None]
        return {
            "days": days,
            "overall": _rate(rows),
            "by_map": _grouped("map"),
            "by_loadout": _grouped("loadout"),
            "death_spots": dict(sorted(deaths.items(), key=lambda kv: -kv[1])),
            "loot": {
                "runs_with_value": len(loot),
                "total": sum(loot),
                "average": round(sum(loot) / len(loot)) if loot else None,
            },
        }

    # ── patch diff ──────────────────────────────────────────────────
    @mcp.tool(
        annotations=READ_ONLY_LOCAL,
        name="arc_patch_diff",
        description=(
            "What changed in the item/weapon data since N days ago, from "
            "locally stored snapshots: value, rarity, and stat changes "
            "plus added/removed items. Turns 'balance shifts every patch' "
            "hedging into 'Kettle damage changed on <date>'. Snapshots "
            "accumulate automatically as the tools are used, so history "
            "starts from first deployment."
        ),
    )
    async def patch_diff(
        since_days: Annotated[
            int, Field(ge=1, le=180, description="Compare against ~N days ago.")
        ] = 7,
    ) -> dict[str, Any]:
        if store is None:
            return _store_error()
        # Ensure today's snapshot exists before diffing (the fetch's
        # side effect); a dead upstream still diffs stored history.
        with contextlib.suppress(ToolError):
            await _all_items()
        latest = await store.latest_snapshot("items")
        if latest is None:
            return {
                "changes": [],
                "hint": (
                    "No snapshots stored yet — they accumulate automatically "
                    "as the item tools are used. Check back tomorrow."
                ),
            }
        baseline = await store.snapshot_at_or_before("items", time.time() - since_days * 86400)
        assert baseline is not None  # latest exists, so the fallback returns it
        base_ts, base = baseline
        new_ts, new = latest
        if base_ts == new_ts:
            return {
                "changes": [],
                "snapshots_stored": await store.snapshot_count("items"),
                "hint": (
                    "Only one distinct snapshot so far — no baseline older "
                    f"than {since_days}d to compare against yet."
                ),
            }
        changes: list[dict[str, Any]] = []
        for name in sorted(set(base) | set(new)):
            if name not in base:
                changes.append({"item": name, "change": "added"})
                continue
            if name not in new:
                changes.append({"item": name, "change": "removed"})
                continue
            old_row, new_row = base[name], new[name]
            for field in ("value", "rarity", "type"):
                if old_row.get(field) != new_row.get(field):
                    changes.append(
                        {
                            "item": name,
                            "field": field,
                            "old": old_row.get(field),
                            "new": new_row.get(field),
                        }
                    )
            old_stats, new_stats = old_row.get("stats") or {}, new_row.get("stats") or {}
            for stat in sorted(set(old_stats) | set(new_stats)):
                if old_stats.get(stat) != new_stats.get(stat):
                    changes.append(
                        {
                            "item": name,
                            "field": f"stats.{stat}",
                            "old": old_stats.get(stat),
                            "new": new_stats.get(stat),
                        }
                    )
        truncated = len(changes) > 200
        return {
            "baseline_utc": _iso_utc(base_ts * 1000),
            "latest_utc": _iso_utc(new_ts * 1000),
            "changes": changes[:200],
            "returned": min(len(changes), 200),
            "total": len(changes),
            "truncated": truncated,
            "source": METAFORGE_SOURCE,
        }

    # ── event schedule ──────────────────────────────────────────────
    @mcp.tool(
        annotations=READ_ONLY,
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
                    "map_url": _mapgenie_url(ev.get("map")),
                    "status": status,
                    "starts_utc": _iso_utc(start),
                    "ends_utc": _iso_utc(end),
                }
            )
        return {"events": events, "returned": len(events), "source": METAFORGE_SOURCE}

    # ── maps ────────────────────────────────────────────────────────
    @mcp.tool(
        annotations=READ_ONLY,
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
                "mapgenie_url": _mapgenie_url((m.get("name") or {}).get("en") or m.get("id")),
            }
            for m in (raw if isinstance(raw, list) else [])
        ]
        return {"maps": maps, "returned": len(maps), "source": RAIDTHEORY_SOURCE}

    # ── wiki ────────────────────────────────────────────────────────
    @mcp.tool(
        annotations=READ_ONLY,
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
        annotations=READ_ONLY,
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
