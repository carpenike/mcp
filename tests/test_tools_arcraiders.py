"""ARC Raiders game-data tool tests.

Exercises the MetaForge/RaidTheory/wiki projections, the structured-error
contract (no raise to the transport), the fixed-URL TTL cache, and the
truncation flags. All upstream HTTP is mocked (AGENTS testing rules).
"""

from __future__ import annotations

import time
from collections.abc import Callable
from typing import Any

import pytest
from pytest_httpx import HTTPXMock

from homelab_mcp.config import Settings
from homelab_mcp.tools.arcraiders import register

METAFORGE = "https://metaforge.test/api/arc-raiders"
DATA = "https://data.test/main"
DATA_LISTING = "https://ghapi.test/contents"
ARDB = "https://ardb.test/api"
WIKI = "https://wiki.test/w/api.php"

pytestmark = pytest.mark.httpx_mock(assert_all_responses_were_requested=False)


class CapturingMCP:
    """Collects tools registered via @mcp.tool(name=...) so tests can call them."""

    def __init__(self) -> None:
        self.tools: dict[str, Callable[..., Any]] = {}
        self.annotations: dict[str, Any] = {}

    def tool(
        self, *, name: str, description: str = "", annotations: Any = None
    ) -> Callable[..., Any]:
        def deco(fn: Callable[..., Any]) -> Callable[..., Any]:
            self.tools[name] = fn
            self.annotations[name] = annotations
            return fn

        return deco


# The only arc tools allowed to write, and the only one allowed to be
# destructive (the confirm-gated raid-log correction path). Everything
# else in the category must stay read-only.
_EXPECTED_WRITERS = {"arc_log_raid", "arc_delete_raid"}
_EXPECTED_DESTRUCTIVE = {"arc_delete_raid"}


def test_tool_annotation_posture(monkeypatch: pytest.MonkeyPatch) -> None:
    """Every arc_* tool must declare annotations, and only the raid-log
    writers may deviate from read-only — claude.ai's permission UI groups
    by these hints."""
    monkeypatch.setenv("HOMELAB_MCP_OAUTH_REQUIRED", "false")
    monkeypatch.setenv("HOMELAB_MCP_ARCRAIDERS_DB_PATH", ":memory:")
    mcp = CapturingMCP()
    register(mcp, Settings())  # type: ignore[arg-type]
    assert mcp.tools, "no tools registered"
    for name, ann in mcp.annotations.items():
        assert ann is not None, f"{name} has no annotations"
        assert ann.readOnlyHint is (name not in _EXPECTED_WRITERS), f"{name} readOnlyHint"
        assert ann.destructiveHint is (name in _EXPECTED_DESTRUCTIVE), f"{name} destructiveHint"


@pytest.fixture
def tools(monkeypatch: pytest.MonkeyPatch) -> dict[str, Callable[..., Any]]:
    monkeypatch.setenv("HOMELAB_MCP_OAUTH_REQUIRED", "false")
    monkeypatch.setenv("HOMELAB_MCP_ARCRAIDERS_METAFORGE_BASE_URL", METAFORGE)
    monkeypatch.setenv("HOMELAB_MCP_ARCRAIDERS_DATA_BASE_URL", DATA)
    monkeypatch.setenv("HOMELAB_MCP_ARCRAIDERS_DATA_LISTING_URL", DATA_LISTING)
    monkeypatch.setenv("HOMELAB_MCP_ARCRAIDERS_ARDB_BASE_URL", ARDB)
    monkeypatch.setenv("HOMELAB_MCP_ARCRAIDERS_WIKI_API_URL", WIKI)
    monkeypatch.setenv("HOMELAB_MCP_ARCRAIDERS_DB_PATH", ":memory:")
    mcp = CapturingMCP()
    register(mcp, Settings())  # type: ignore[arg-type]
    return mcp.tools


def _item(name: str, **overrides: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "id": name.lower().replace(" ", "-"),
        "name": name,
        "item_type": "Ammunition",
        "subcategory": "Ammunition",
        "rarity": "Common",
        "value": 12,
        "description": "desc",
        "workbench": "Workbench",
        "stat_block": {"weight": 0.05, "damage": 0, "stackSize": 40, "shieldCompatibility": ""},
        "article": "<p>huge html blob that must not be returned</p>",
        "updated_at": "2026-06-29T16:23:59Z",
    }
    base.update(overrides)
    return base


# ── client instructions ──────────────────────────────────────────────


def test_instructions_are_collected_for_clients() -> None:
    """The module's INSTRUCTIONS must reach FastMCP via the registry collector."""
    from homelab_mcp.tools import collect_instructions

    text = collect_instructions()
    assert text is not None
    # Workflow guidance and the honest pull-only caveat must both survive.
    assert "arc_check_item_keep" in text
    assert "pull-only" in text


# ── items ────────────────────────────────────────────────────────────


async def test_search_items_projects_and_drops_zero_stats(
    tools: dict[str, Callable[..., Any]], httpx_mock: HTTPXMock
) -> None:
    httpx_mock.add_response(
        url=f"{METAFORGE}/items?search=heavy&limit=10&page=1",
        json={
            "data": [_item("Heavy Ammo")],
            "pagination": {"page": 1, "limit": 10, "total": 37, "hasNextPage": True},
        },
    )
    out = await tools["arc_search_items"](query="heavy")
    assert out["returned"] == 1
    assert out["total"] == 37
    assert out["truncated"] is True
    item = out["items"][0]
    assert item["name"] == "Heavy Ammo"
    # Zero/empty stats and the html article blob are projected away.
    assert item["stats"] == {"weight": 0.05, "stackSize": 40}
    assert "article" not in item
    assert item["loot_area"] is None
    # Populate-or-drop: sparse fields are OMITTED when empty, and
    # subcategory (always a duplicate of type) is gone entirely.
    assert "found_on_maps" not in item
    assert "subcategory" not in item
    assert "MetaForge" in out["source"]


async def test_search_items_error_is_structured_not_raised(
    tools: dict[str, Callable[..., Any]], httpx_mock: HTTPXMock
) -> None:
    """A 503 upstream must return an {error:...} payload, not raise (AGENTS rule 4)."""
    httpx_mock.add_response(url=f"{METAFORGE}/items?search=x&limit=10&page=1", status_code=503)
    out = await tools["arc_search_items"](query="x")
    assert out["error"]["code"] == "metaforge_http_503"


# ── quests ───────────────────────────────────────────────────────────


async def test_search_quests_projects_rewards(
    tools: dict[str, Callable[..., Any]], httpx_mock: HTTPXMock
) -> None:
    httpx_mock.add_response(
        url=f"{METAFORGE}/quests?limit=10&page=1&search=bad",
        json={
            "data": [
                {
                    "id": "a-bad-feeling",
                    "name": "A Bad Feeling",
                    "trader_name": "Celeste",
                    "objectives": ["Find and search any ARC Probe"],
                    "xp": 0,
                    "guide_url": "/arc-raiders/a-bad-feeling",
                    "required_items": [
                        {
                            "item": {"id": "wires", "name": "Wires", "rarity": "Common"},
                            "item_id": "wires",
                            "quantity": 5,
                        }
                    ],
                    "rewards": [
                        {
                            "item": {"id": "spring", "name": "Steel Spring", "rarity": "Uncommon"},
                            "item_id": "spring",
                            "quantity": "5",
                        }
                    ],
                }
            ],
            "pagination": {"total": 1, "hasNextPage": False},
        },
    )
    out = await tools["arc_search_quests"](query="bad")
    quest = out["quests"][0]
    assert quest["trader"] == "Celeste"
    assert quest["rewards"] == [{"item": "Steel Spring", "rarity": "Uncommon", "quantity": "5"}]
    assert quest["required_items"] == [{"item": "Wires", "rarity": "Common", "quantity": 5}]
    assert quest["guide_url"] == "https://metaforge.app/arc-raiders/a-bad-feeling"
    assert out["truncated"] is False


# ── traders ──────────────────────────────────────────────────────────


def _traders_body() -> dict[str, Any]:
    return {
        "success": True,
        "data": {
            "Apollo": [
                {
                    "id": "barricade-kit",
                    "name": "Barricade Kit",
                    "item_type": "Quick Use",
                    "rarity": "Uncommon",
                    "value": 640,
                    "trader_price": 1920,
                }
            ],
            "Celeste": [],
        },
    }


async def test_trader_stock_filters_case_insensitively(
    tools: dict[str, Callable[..., Any]], httpx_mock: HTTPXMock
) -> None:
    httpx_mock.add_response(url=f"{METAFORGE}/traders", json=_traders_body())
    out = await tools["arc_get_trader_stock"](trader="apollo")
    assert list(out["traders"]) == ["Apollo"]
    assert out["traders"]["Apollo"][0]["price"] == 1920


async def test_trader_stock_unknown_trader_lists_known(
    tools: dict[str, Callable[..., Any]], httpx_mock: HTTPXMock
) -> None:
    httpx_mock.add_response(url=f"{METAFORGE}/traders", json=_traders_body())
    out = await tools["arc_get_trader_stock"](trader="nope")
    assert out["error"]["code"] == "metaforge_unknown_trader"
    assert "Apollo" in out["error"]["hint"]


async def test_trader_stock_second_call_served_from_cache(
    tools: dict[str, Callable[..., Any]], httpx_mock: HTTPXMock
) -> None:
    httpx_mock.add_response(url=f"{METAFORGE}/traders", json=_traders_body())
    await tools["arc_get_trader_stock"]()
    await tools["arc_get_trader_stock"]()
    assert len(httpx_mock.get_requests()) == 1


# ── keep-or-sell ─────────────────────────────────────────────────────


async def test_check_item_keep_aggregates_all_sources(
    tools: dict[str, Callable[..., Any]], httpx_mock: HTTPXMock
) -> None:
    # Fuzzy hit first, exact-normalized second: the tool must pick the exact one.
    httpx_mock.add_response(
        url=f"{METAFORGE}/items?limit=100&page=1",
        json={
            "data": [
                _item("ARC Alloy Cluster", id="arc-alloy-cluster"),
                _item(
                    "ARC Alloy",
                    id="arc-alloy",
                    value=90,
                    loot_area="ARC",
                    locations=[{"id": "m1", "map": "dam"}, {"id": "m2", "map": "spaceport"}],
                ),
                _item("Metal Parts", id="metal-parts", value=75),
            ],
            "pagination": {"hasNextPage": False},
        },
    )
    httpx_mock.add_response(
        url=f"{DATA}/projects.json",
        json=[
            {
                "id": "trophy_display",
                "disabled": False,
                "endDate": 2524521600,
                "name": {"en": "Trophy Display"},
                "phases": [
                    {"phase": 1, "requirementItemIds": [{"itemId": "arc_alloy", "quantity": 4}]}
                ],
            },
            {
                "id": "expired_project",
                "disabled": False,
                "endDate": 1,
                "name": {"en": "Expired"},
                "phases": [
                    {"phase": 1, "requirementItemIds": [{"itemId": "arc_alloy", "quantity": 99}]}
                ],
            },
        ],
    )
    httpx_mock.add_response(
        url=f"{DATA}/items/arc_alloy.json",
        json={
            "value": 200,
            "recyclesInto": {"metal_parts": 2},
            "salvagesInto": {},
            "tip": "Dropped by ARC units.",
        },
    )
    httpx_mock.add_response(
        url=f"{ARDB}/items",
        json=[{"id": "arc_alloy", "name": "ARC Alloy"}],
    )
    httpx_mock.add_response(
        url=f"{ARDB}/items/arc_alloy",
        json={
            "id": "arc_alloy",
            "name": "ARC Alloy",
            "value": 200,
            "usedInCraft": [{"id": "light_shield", "name": "Light Shield"}],
            "droppedBy": [{"id": "wasp", "name": "Wasp"}],
            "craftingRequirement": None,
        },
    )
    httpx_mock.add_response(
        url=f"{METAFORGE}/quests?limit=100&page=1",
        json={
            "data": [
                {
                    "name": "Supply Run",
                    "trader_name": "Celeste",
                    "required_items": [
                        {"item": {"name": "ARC Alloy"}, "item_id": "arc-alloy", "quantity": 6}
                    ],
                },
                {
                    "name": "Unrelated",
                    "trader_name": "Apollo",
                    "required_items": [
                        {"item": {"name": "Wires"}, "item_id": "wires", "quantity": 5}
                    ],
                },
            ],
            "pagination": {"hasNextPage": False},
        },
    )
    httpx_mock.add_response(
        url=f"{DATA_LISTING}/hideout",
        json=[
            {"name": "med_station.json", "type": "file"},
            {"name": "workbench.json", "type": "file"},
        ],
    )
    httpx_mock.add_response(
        url=f"{DATA}/hideout/workbench.json",
        json={"id": "workbench", "name": {"en": "Workbench"}, "maxLevel": 0, "levels": []},
    )
    httpx_mock.add_response(
        url=f"{DATA}/hideout/med_station.json",
        json={
            "id": "med_station",
            "name": {"en": "Medical Lab"},
            "levels": [
                {
                    "level": 2,
                    "requirementItemIds": [
                        {"itemId": "arc_alloy", "quantity": 12},
                        {"itemId": "fabric", "quantity": 50},
                    ],
                }
            ],
        },
    )
    httpx_mock.add_response(
        url=f"{METAFORGE}/traders",
        json={
            "success": True,
            "data": {
                "Apollo": [
                    {"id": "arc-alloy", "name": "ARC Alloy", "trader_price": 270},
                    {"id": "wires", "name": "Wires", "trader_price": 30},
                ]
            },
        },
    )
    httpx_mock.add_response(
        url=(
            f"{WIKI}?action=query&prop=extracts&titles=ARC%20Alloy&explaintext=1"
            "&redirects=1&format=json"
        ),
        json={
            "query": {
                "pages": {
                    "7": {
                        "pageid": 7,
                        "title": "ARC Alloy",
                        "extract": (
                            "Alloy prose.\n\n== Location ==\n"
                            "West of Olive Grove\n\nIn-Game location\n\n== History =="
                        ),
                    }
                }
            }
        },
    )
    out = await tools["arc_check_item_keep"](item="ARC Alloy")
    assert out["match"] == "exact"
    assert out["item"]["name"] == "ARC Alloy"
    assert out["other_candidates"] == ["ARC Alloy Cluster"]
    assert out["quests_requiring"] == [{"quest": "Supply Run", "trader": "Celeste", "quantity": 6}]
    # Snake_case RaidTheory id matched against the MetaForge display name.
    assert out["hideout_requiring"] == [{"module": "Medical Lab", "level": 2, "quantity": 12}]
    assert out["trader_offers"] == [{"trader": "Apollo", "price": 270}]
    assert out["item"]["loot_area"] == "ARC"
    assert out["item"]["found_on_maps"] == ["dam", "spaceport"]
    # MapGenie links open pre-filtered to the item via ?search=.
    assert out["map_links"] == {
        "dam": "https://mapgenie.io/arc-raiders/maps/dam-battlegrounds?search=ARC%20Alloy",
        "spaceport": "https://mapgenie.io/arc-raiders/maps/spaceport?search=ARC%20Alloy",
    }
    assert out["wiki_location"] == "West of Olive Grove"
    # Projects axis: active project counted, expired project excluded.
    # 2049 is a sentinel ("no deadline"), never presented as a real date.
    assert out["projects_requiring"] == [
        {
            "project": "Trophy Display",
            "description": None,
            "phase": 1,
            "quantity": 4,
            "ends_utc": None,
            "permanent": True,
        }
    ]
    # Verdict sums all three axes: hideout 12 + quest 6 + project 4.
    assert out["verdict"] == "keep"
    assert out["keep_quantity"] == 22
    assert "Trophy Display" in out["verdict_reason"]
    assert out["recycles_to"] == [{"item": "Metal Parts", "quantity": 2, "value_each": 75}]
    assert out["salvages_to"] == []
    assert out["recycle_value_delta"] == 150 - 90
    assert out["item"]["tip"] == "Dropped by ARC units."
    assert out["coverage"] == {
        "quests": "complete",
        "hideout": "complete",
        "projects": "complete",
        "crafting_recipes": "complete",
        "events": "not_modeled",
    }
    assert out["weapon_specs"] is None
    assert out["crafting_uses"] == ["Light Shield"]
    assert out["dropped_by"] == ["Wasp"]
    assert any("ardb.app" in src for src in out["sources"])
    assert out["notes"] == []


@pytest.mark.httpx_mock(
    assert_all_responses_were_requested=False, assert_all_requests_were_expected=False
)
async def test_check_item_keep_degrades_per_source(
    tools: dict[str, Callable[..., Any]], httpx_mock: HTTPXMock
) -> None:
    """Failures in quests/hideout/traders must degrade to None + note, not error."""
    httpx_mock.add_response(
        url=f"{METAFORGE}/items?limit=100&page=1",
        json={"data": [_item("Wires", id="wires")], "pagination": {"hasNextPage": False}},
    )
    httpx_mock.add_response(url=f"{METAFORGE}/quests?limit=100&page=1", status_code=503)
    # Hideout listing and all fallback module fetches are unmatched → httpx
    # timeouts → skipped; traders unmatched → timeout → note.
    out = await tools["arc_check_item_keep"](item="wires")
    assert out["item"]["name"] == "Wires"
    assert out["quests_requiring"] is None
    assert out["hideout_requiring"] is None
    assert out["projects_requiring"] is None
    assert out["trader_offers"] is None
    # Wiki lookup failure is silent (many items have no page); the empty
    # field is omitted, not null.
    assert "wiki_location" not in out
    # quests + hideout + projects + traders + recycle-detail = 5 notes.
    assert len(out["notes"]) == 5
    assert out["coverage"]["quests"] == "unavailable"
    assert out["coverage"]["hideout"] == "unavailable"
    assert out["coverage"]["projects"] == "unavailable"
    assert out["coverage"]["crafting_recipes"] == "unavailable"
    assert out["weapon_specs"] is None and out["crafting_uses"] is None
    # The verdict must hedge when whole axes were unavailable.
    # Common item: verdict stays "sell" (degradation is Epic+ only).
    assert out["verdict"] == "sell"
    assert "unavailable" in out["verdict_reason"]
    assert "events" in out["verdict_reason"]  # not-modeled axes named on every sell


@pytest.mark.httpx_mock(
    assert_all_responses_were_requested=False, assert_all_requests_were_expected=False
)
async def test_check_item_keep_degrades_verdict_for_epic_blind_spots(
    tools: dict[str, Callable[..., Any]], httpx_mock: HTTPXMock
) -> None:
    """Epic+ item, no demand, events not modeled -> probably_sell, never a
    confident sell (v3: the hedge must live in the field callers act on)."""
    httpx_mock.add_response(
        url=f"{METAFORGE}/items?limit=100&page=1",
        json={
            "data": [_item("Fancy Obelisk", id="fancy-obelisk", rarity="Legendary", value=9000)],
            "pagination": {"hasNextPage": False},
        },
    )
    httpx_mock.add_response(
        url=f"{METAFORGE}/quests?limit=100&page=1", json={"data": [], "pagination": {}}
    )
    httpx_mock.add_response(url=f"{DATA}/projects.json", json=[])
    out = await tools["arc_check_item_keep"](item="Fancy Obelisk")
    assert out["verdict"] == "probably_sell"
    assert "verify no active event" in out["verdict_reason"]


async def test_check_item_keep_unknown_item(
    tools: dict[str, Callable[..., Any]], httpx_mock: HTTPXMock
) -> None:
    httpx_mock.add_response(
        url=f"{METAFORGE}/items?limit=100&page=1",
        json={"data": [_item("Wires")], "pagination": {"hasNextPage": False}},
    )
    out = await tools["arc_check_item_keep"](item="nope")
    assert out["error"]["code"] == "metaforge_item_not_found"


@pytest.mark.httpx_mock(
    assert_all_responses_were_requested=False, assert_all_requests_were_expected=False
)
async def test_check_item_keep_resolves_spacing_variants(
    tools: dict[str, Callable[..., Any]], httpx_mock: HTTPXMock
) -> None:
    """'lightbulb' must resolve to 'Light Bulb' despite the spacing mismatch."""
    httpx_mock.add_response(
        url=f"{METAFORGE}/items?limit=100&page=1",
        json={
            "data": [_item("Blue Light Stick"), _item("Light Bulb", id="light-bulb")],
            "pagination": {"hasNextPage": False},
        },
    )
    out = await tools["arc_check_item_keep"](item="lightbulb")
    assert out["item"]["name"] == "Light Bulb"
    assert out["match"] == "exact"


async def test_search_items_falls_back_to_local_fuzzy(
    tools: dict[str, Callable[..., Any]], httpx_mock: HTTPXMock
) -> None:
    """Server word-search misses 'lightbulb'; the local squash-match must not."""
    httpx_mock.add_response(
        url=f"{METAFORGE}/items?search=lightbulb&limit=10&page=1",
        json={"data": [], "pagination": {"hasNextPage": False}},
    )
    httpx_mock.add_response(
        url=f"{METAFORGE}/items?limit=100&page=1",
        json={
            "data": [_item("Light Bulb", id="light-bulb"), _item("Blue Light Stick")],
            "pagination": {"hasNextPage": False},
        },
    )
    out = await tools["arc_search_items"](query="lightbulb")
    assert out["returned"] == 1
    assert out["items"][0]["name"] == "Light Bulb"
    assert "locally" in out["note"]


# ── upgrade planner ──────────────────────────────────────────────────


def _mock_planner_world(httpx_mock: HTTPXMock) -> None:
    """One hideout module (Medical Lab, max L3) + the item table + traders."""
    httpx_mock.add_response(
        url=f"{DATA_LISTING}/hideout",
        json=[
            {"name": "med_station.json", "type": "file"},
            {"name": "workbench.json", "type": "file"},
        ],
    )
    httpx_mock.add_response(
        url=f"{DATA}/hideout/workbench.json",
        json={"id": "workbench", "name": {"en": "Workbench"}, "maxLevel": 0, "levels": []},
    )
    httpx_mock.add_response(
        url=f"{DATA}/hideout/med_station.json",
        json={
            "id": "med_station",
            "name": {"en": "Medical Lab"},
            "maxLevel": 3,
            "levels": [
                {"level": 1, "requirementItemIds": [{"itemId": "fabric", "quantity": 50}]},
                {
                    "level": 2,
                    "requirementItemIds": [
                        {"itemId": "fabric", "quantity": 20},
                        {"itemId": "arc_alloy", "quantity": 6},
                    ],
                },
                {"level": 3, "requirementItemIds": [{"itemId": "arc_alloy", "quantity": 12}]},
            ],
        },
    )
    httpx_mock.add_response(
        url=f"{METAFORGE}/items?limit=100&page=1",
        json={
            "data": [
                _item("Fabric", id="fabric", value=10, rarity="Common"),
                _item("ARC Alloy", id="arc-alloy", value=200, rarity="Uncommon"),
            ],
            "pagination": {"hasNextPage": False},
        },
    )
    httpx_mock.add_response(url=f"{METAFORGE}/traders", json={"success": True, "data": {}})


async def test_plan_upgrades_without_stash_uses_nulls(
    tools: dict[str, Callable[..., Any]], httpx_mock: HTTPXMock
) -> None:
    """No stash -> pool/short are null (unknown), never fabricated zeros."""
    _mock_planner_world(httpx_mock)
    out = await tools["arc_plan_upgrades"](current_levels={"Medical Lab": 1})
    (plan,) = out["modules"]
    assert (plan["module"], plan["from"], plan["to"], plan["action"]) == (
        "Medical Lab",
        1,
        2,
        "upgrade",
    )
    by_item = {r["item"]: r for r in plan["requirements"]}
    assert by_item["Fabric"] == {
        "item": "Fabric",
        "need": 20,
        "pool": None,
        "short": None,
        "rarity": "Common",
    }
    assert plan["units_outstanding"] == 26  # falls back to total need
    # Workbench (maxLevel 0, nothing to plan) must NOT pollute the
    # unknown-level list even though it wasn't in current_levels.
    assert out["modules_with_unknown_level"] == []
    assert out["coverage"]["quests"] == "not_included"
    assert out["coverage"]["projects"] == "not_included"


async def test_plan_upgrades_multi_level_with_stash(
    tools: dict[str, Callable[..., Any]], httpx_mock: HTTPXMock
) -> None:
    """Target jumps are cumulative with per-level breakdown; stash is a
    shared pool with fuzzy-but-safe key resolution."""
    _mock_planner_world(httpx_mock)
    out = await tools["arc_plan_upgrades"](
        current_levels={"Medical Lab": 1},
        targets={"Medical Lab": 3},
        stash={"Fabric": 60, "arc alloys": 3, "xyzzy": 5},
    )
    (plan,) = out["modules"]
    assert plan["to"] == 3
    assert [lv["level"] for lv in plan["per_level"]] == [2, 3]
    by_item = {r["item"]: r for r in plan["requirements"]}
    # Cumulative: arc_alloy 6 (L2) + 12 (L3) = 18; plural 'arc alloys' resolved.
    assert by_item["ARC Alloy"] == {
        "item": "ARC Alloy",
        "need": 18,
        "pool": 3,
        "short": 15,
        "rarity": "Uncommon",
    }
    assert by_item["Fabric"]["short"] == 0
    # Shopping list drops satisfied items, keeps shortfalls.
    items_listed = [s["item"] for s in out["shopping_list"]]
    assert "ARC Alloy" in items_listed and "Fabric" not in items_listed
    # Unresolvable key is returned, never silently dropped.
    assert out["unresolved_stash_keys"] == [{"key": "xyzzy", "candidates": []}]


async def test_plan_upgrades_quest_scoping_and_provenance(
    tools: dict[str, Callable[..., Any]], httpx_mock: HTTPXMock
) -> None:
    """active_quests restricts folding to accepted quests; unmatched names
    are returned; every shopping line explains itself via required_by."""
    _mock_planner_world(httpx_mock)
    httpx_mock.add_response(
        url=f"{METAFORGE}/quests?limit=100&page=1",
        json={
            "data": [
                {
                    "name": "The Trifecta",
                    "required_items": [
                        {"item": {"name": "ARC Alloy"}, "item_id": "arc-alloy", "quantity": 2}
                    ],
                },
                {
                    "name": "Unrelated Quest",
                    "required_items": [
                        {
                            "item": {"name": "Deflated Football"},
                            "item_id": "deflated-football",
                            "quantity": 1,
                        }
                    ],
                },
            ],
            "pagination": {"hasNextPage": False},
        },
    )
    out = await tools["arc_plan_upgrades"](
        current_levels={"Medical Lab": 1},
        include_quests=True,
        active_quests=["the trifecta", "Ghost Quest"],
    )
    items = {s["item"] for s in out["shopping_list"]}
    assert "Deflated Football" not in items  # unaccepted quest not folded
    alloy = next(s for s in out["shopping_list"] if s["item"] == "ARC Alloy")
    assert alloy["total_need"] == 8  # 6 hideout + 2 quest
    assert set(alloy["required_by"]) == {"Medical Lab L2", "quest The Trifecta"}
    assert out["unresolved_quest_names"] == ["Ghost Quest"]
    assert out["coverage"]["quests"] == "complete"


async def test_plan_upgrades_folds_active_projects(
    tools: dict[str, Callable[..., Any]], httpx_mock: HTTPXMock
) -> None:
    _mock_planner_world(httpx_mock)
    httpx_mock.add_response(
        url=f"{DATA}/projects.json",
        json=[
            {
                "id": "trophy",
                "disabled": False,
                "endDate": 2524521600,
                "name": {"en": "Trophy Display"},
                "phases": [
                    {"phase": 1, "requirementItemIds": [{"itemId": "fabric", "quantity": 10}]}
                ],
            }
        ],
    )
    out = await tools["arc_plan_upgrades"](current_levels={"Medical Lab": 1}, include_projects=True)
    fabric = next(s for s in out["shopping_list"] if s["item"] == "Fabric")
    assert fabric["total_need"] == 30  # 20 hideout + 10 project
    assert "project Trophy Display phase 1" in fabric["required_by"]
    assert out["coverage"]["projects"] == "complete"


async def test_plan_upgrades_project_phase_scoping(
    tools: dict[str, Callable[..., Any]], httpx_mock: HTTPXMock
) -> None:
    """active_projects folds ONLY the phase the user is on; unmatched
    project names are returned; raw ids render readably (v3-1, v3-3)."""
    _mock_planner_world(httpx_mock)
    httpx_mock.add_response(
        url=f"{DATA}/projects.json",
        json=[
            {
                "id": "trophy",
                "disabled": False,
                "endDate": 2524521600,
                "name": {"en": "Trophy Display"},
                "phases": [
                    {"phase": 1, "requirementItemIds": [{"itemId": "fabric", "quantity": 10}]},
                    {
                        "phase": 5,
                        "requirementItemIds": [{"itemId": "colorful_shoes_red", "quantity": 1}],
                    },
                ],
            }
        ],
    )
    out = await tools["arc_plan_upgrades"](
        current_levels={"Medical Lab": 1},
        include_projects=True,
        active_projects={"trophy display": 1, "Ghost Project": 2},
    )
    items = {s["item"] for s in out["shopping_list"]}
    # Phase 5's cosmetic is NOT folded while the user is on phase 1.
    assert not any("shoes" in i.lower() for i in items)
    fabric = next(s for s in out["shopping_list"] if s["item"] == "Fabric")
    assert fabric["total_need"] == 30  # 20 hideout + 10 phase-1 project
    assert out["unresolved_project_names"] == ["Ghost Project"]

    # Unscoped fold DOES include phase 5, with the id rendered readably.
    out_all = await tools["arc_plan_upgrades"](
        current_levels={"Medical Lab": 1}, include_projects=True
    )
    shoes = next(s for s in out_all["shopping_list"] if s["item"] == "Colorful Shoes Red")
    assert "project Trophy Display phase 5" in shoes["required_by"]
    # Null rarity/loot_area are meaningful here — flagged, not backfilled.
    assert shoes["unresolved"] is True
    assert shoes["rarity"] is None
    fabric = next(s for s in out_all["shopping_list"] if s["item"] == "Fabric")
    assert "unresolved" not in fabric


async def test_plan_upgrades_unknown_module_is_error(
    tools: dict[str, Callable[..., Any]], httpx_mock: HTTPXMock
) -> None:
    _mock_planner_world(httpx_mock)
    out = await tools["arc_plan_upgrades"](current_levels={"Bogus Station": 1})
    assert out["error"]["code"] == "arcraiders_unknown_module"
    assert "Medical Lab" in out["error"]["hint"]


async def test_plan_upgrades_target_below_current_is_error(
    tools: dict[str, Callable[..., Any]], httpx_mock: HTTPXMock
) -> None:
    _mock_planner_world(httpx_mock)
    out = await tools["arc_plan_upgrades"](
        current_levels={"Medical Lab": 2}, targets={"Medical Lab": 1}
    )
    assert out["error"]["code"] == "arcraiders_invalid_target"


async def test_plan_upgrades_at_max_is_complete_not_error(
    tools: dict[str, Callable[..., Any]], httpx_mock: HTTPXMock
) -> None:
    _mock_planner_world(httpx_mock)
    out = await tools["arc_plan_upgrades"](current_levels={"Medical Lab": 3})
    (plan,) = out["modules"]
    assert plan["action"] == "complete"
    assert plan["requirements"] == []
    assert out["nearest_completion"] == []


async def test_plan_upgrades_level_zero_is_build(
    tools: dict[str, Callable[..., Any]], httpx_mock: HTTPXMock
) -> None:
    _mock_planner_world(httpx_mock)
    out = await tools["arc_plan_upgrades"](current_levels={"Medical Lab": 0})
    (plan,) = out["modules"]
    assert plan["action"] == "build"
    assert {r["item"]: r["need"] for r in plan["requirements"]} == {"Fabric": 50}


# ── bestiary & weapons ───────────────────────────────────────────────


def _bots_body() -> list[dict[str, Any]]:
    return [
        {
            "id": "arc_wasp",
            "name": {"en": "Wasp"},
            "type": "Scout",
            "threat": "Moderate",
            "weakness": "Shoot the thruster.",
            "description": {"en": "A flying scout."},
            "maps": ["dam_battlegrounds", "the_spaceport"],
            "destroyXp": 100,
            "lootXp": 50,
            "drops": ["arc_alloy", "wasp_driver"],
            "image": "https://cdn.test/wasp.png",
        }
    ]


async def test_get_enemy_resolves_and_projects(
    tools: dict[str, Callable[..., Any]], httpx_mock: HTTPXMock
) -> None:
    httpx_mock.add_response(url=f"{DATA}/bots.json", json=_bots_body())
    httpx_mock.add_response(
        url=f"{METAFORGE}/items?limit=100&page=1",
        json={
            "data": [_item("ARC Alloy", id="arc-alloy"), _item("Wasp Driver", id="wasp-driver")],
            "pagination": {"hasNextPage": False},
        },
    )
    out = await tools["arc_get_enemy"](name="wasp")
    assert out["name"] == "Wasp"
    assert out["threat"] == "Moderate"
    assert out["weakness"] == "Shoot the thruster."
    # snake_case drop ids resolve to display names via the item table.
    assert out["drops"] == ["ARC Alloy", "Wasp Driver"]
    assert out["maps"] == ["Dam Battlegrounds", "The Spaceport"]
    assert "dam-battlegrounds" in out["map_links"]["Dam Battlegrounds"]


async def test_get_enemy_unknown_lists_known(
    tools: dict[str, Callable[..., Any]], httpx_mock: HTTPXMock
) -> None:
    httpx_mock.add_response(url=f"{DATA}/bots.json", json=_bots_body())
    out = await tools["arc_get_enemy"](name="gundam")
    assert out["error"]["code"] == "arcraiders_unknown_enemy"
    assert "Wasp" in out["error"]["hint"]


async def test_who_drops_merges_bots_and_ardb(
    tools: dict[str, Callable[..., Any]], httpx_mock: HTTPXMock
) -> None:
    httpx_mock.add_response(
        url=f"{METAFORGE}/items?limit=100&page=1",
        json={
            "data": [_item("ARC Alloy", id="arc-alloy")],
            "pagination": {"hasNextPage": False},
        },
    )
    httpx_mock.add_response(url=f"{DATA}/bots.json", json=_bots_body())
    httpx_mock.add_response(url=f"{ARDB}/items", json=[{"id": "arc_alloy", "name": "ARC Alloy"}])
    httpx_mock.add_response(
        url=f"{ARDB}/items/arc_alloy",
        json={
            "id": "arc_alloy",
            "name": "ARC Alloy",
            # Wasp duplicates bots.json (must dedupe); Hornet is ardb-only.
            "droppedBy": [{"id": "wasp", "name": "Wasp"}, {"id": "hornet", "name": "Hornet"}],
        },
    )
    out = await tools["arc_who_drops"](item="arc alloy")
    assert out["item"] == "ARC Alloy"
    enemies = {d["enemy"]: d for d in out["dropped_by"]}
    assert set(enemies) == {"Wasp", "Hornet"}
    assert enemies["Wasp"]["threat"] == "Moderate"  # from bots.json, not the bare ardb row


async def test_compare_weapons_normalizes_and_flags_non_weapons(
    tools: dict[str, Callable[..., Any]], httpx_mock: HTTPXMock
) -> None:
    httpx_mock.add_response(
        url=f"{ARDB}/items",
        json=[
            {"id": "ferro", "name": "Ferro I"},
            {"id": "osprey", "name": "Osprey I"},
            {"id": "arc_alloy", "name": "ARC Alloy"},
        ],
    )
    httpx_mock.add_response(
        url=f"{ARDB}/items/ferro",
        json={
            "id": "ferro",
            "name": "Ferro I",
            "rarity": "common",
            "value": 475,
            "weight": 8,
            "weaponSpecs": {
                "armorPenetration": "strong",
                "ammoType": "heavy",
                "firingMode": "break-action",
                "magSize": 1,
                "stats": {"damage": 40, "range": 53.1, "fireRate": 6.6},
            },
        },
    )
    httpx_mock.add_response(
        url=f"{ARDB}/items/arc_alloy",
        json={"id": "arc_alloy", "name": "ARC Alloy", "value": 200},
    )
    out = await tools["arc_compare_weapons"](weapons=["Ferro I", "ARC Alloy"])
    assert out["returned"] == 1
    row = out["weapons"][0]
    assert row["name"] == "Ferro I"
    assert row["armor_penetration"] == "strong"
    assert row["damage"] == 40
    assert row["mag_size"] == 1
    # The non-weapon is flagged, not silently dropped.
    assert any("no weapon specs" in n for n in out["notes"])


# ── raid log & patch diff ────────────────────────────────────────────


async def test_raid_log_roundtrip_and_stats(tools: dict[str, Callable[..., Any]]) -> None:
    """Log -> list -> stats over the in-memory store; no upstream HTTP."""
    for kwargs in (
        {"map_name": "dam", "outcome": "extracted", "loadout": "Ferro IV", "loot_value": 12000},
        {"map_name": "dam", "outcome": "died", "loadout": "Ferro IV", "died_at": "Scrapyard"},
        {"map_name": "Spaceport", "outcome": "extracted", "loadout": "Osprey I"},
    ):
        out = await tools["arc_log_raid"](**kwargs)
        assert out["logged"] is True
    # 'dam' normalized to the display name via the alias table.
    assert out["logged"] is True
    listed = await tools["arc_list_raids"](limit=10)
    assert listed["returned"] == 3
    assert listed["raids"][0]["map"] == "The Spaceport" or listed["raids"][0]["map"] == "Spaceport"
    stats = await tools["arc_raid_stats"](days=30)
    assert stats["overall"] == {"raids": 3, "extracted": 2, "extraction_rate": 0.67}
    assert stats["by_map"]["Dam Battlegrounds"]["raids"] == 2
    assert stats["by_loadout"]["Ferro IV"]["extraction_rate"] == 0.5
    assert stats["death_spots"] == {"Scrapyard": 1}
    assert stats["loot"]["total"] == 12000


async def test_raid_log_invalid_outcome(tools: dict[str, Callable[..., Any]]) -> None:
    out = await tools["arc_log_raid"](map_name="dam", outcome="rage_quit")
    assert out["error"]["code"] == "arcraiders_invalid_outcome"


async def test_delete_raid_previews_without_confirm(
    tools: dict[str, Callable[..., Any]],
) -> None:
    logged = await tools["arc_log_raid"](map_name="dam", outcome="died")
    preview = await tools["arc_delete_raid"](raid_id=logged["id"])
    assert preview["deleted"] is False
    assert preview["preview"]["outcome"] == "died"
    done = await tools["arc_delete_raid"](raid_id=logged["id"], confirm=True)
    assert done["deleted"] is True
    assert (await tools["arc_list_raids"]())["returned"] == 0


async def test_patch_diff_reports_changes(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Any, httpx_mock: HTTPXMock
) -> None:
    """A backdated baseline snapshot + fresh fetch -> concrete stat diffs."""
    from homelab_mcp.arcraiders_state import ArcState

    db = str(tmp_path / "arc.db")
    seed = ArcState(db)
    import asyncio as _asyncio

    _asyncio.get_event_loop()  # ensure a loop exists for the sync seed below
    # Seed a 10-day-old baseline directly.
    seed._conn.execute(
        "INSERT INTO snapshot (ts, kind, content, content_hash) VALUES (?, ?, ?, ?)",
        (
            time.time() - 10 * 86400,
            "items",
            '{"Kettle": {"value": 100, "rarity": "Rare", "type": "AR", "stats": {"damage": 30}}}',
            "seedhash",
        ),
    )
    seed._conn.commit()

    monkeypatch.setenv("HOMELAB_MCP_OAUTH_REQUIRED", "false")
    monkeypatch.setenv("HOMELAB_MCP_ARCRAIDERS_METAFORGE_BASE_URL", METAFORGE)
    monkeypatch.setenv("HOMELAB_MCP_ARCRAIDERS_DB_PATH", db)
    mcp = CapturingMCP()
    register(mcp, Settings())  # type: ignore[arg-type]
    httpx_mock.add_response(
        url=f"{METAFORGE}/items?limit=100&page=1",
        json={
            "data": [
                _item(
                    "Kettle",
                    id="kettle",
                    value=100,
                    rarity="Rare",
                    item_type="AR",
                    stat_block={"damage": 35},
                ),
            ],
            "pagination": {"hasNextPage": False},
        },
    )
    out = await mcp.tools["arc_patch_diff"](since_days=7)
    assert {"item": "Kettle", "field": "stats.damage", "old": 30, "new": 35} in out["changes"]
    assert out["truncated"] is False


# ── events ───────────────────────────────────────────────────────────


async def test_event_schedule_status_and_map_filter(
    tools: dict[str, Callable[..., Any]], httpx_mock: HTTPXMock
) -> None:
    now_ms = time.time() * 1000
    hour = 3_600_000
    httpx_mock.add_response(
        url=f"{METAFORGE}/events-schedule",
        json={
            "data": [
                {
                    "name": "Harvester",
                    "map": "Spaceport",
                    "startTime": now_ms - hour,
                    "endTime": now_ms + hour,
                },
                {
                    "name": "Night Raid",
                    "map": "Dam",
                    "startTime": now_ms + hour,
                    "endTime": now_ms + 2 * hour,
                },
                {
                    "name": "Hurricane",
                    "map": "Dam",
                    "startTime": now_ms - 2 * hour,
                    "endTime": now_ms - hour,
                },
            ]
        },
    )
    out = await tools["arc_get_event_schedule"]()
    statuses = {e["name"]: e["status"] for e in out["events"]}
    # The ended event is dropped by default.
    assert statuses == {"Harvester": "active", "Night Raid": "upcoming"}

    filtered = await tools["arc_get_event_schedule"](map_name="dam", include_past=True)
    assert {e["name"] for e in filtered["events"]} == {"Night Raid", "Hurricane"}
    assert all(e["map"] == "Dam" for e in filtered["events"])
    assert all(
        e["map_url"] == "https://mapgenie.io/arc-raiders/maps/dam-battlegrounds"
        for e in filtered["events"]
    )


# ── maps ─────────────────────────────────────────────────────────────


async def test_list_maps_projects_english_name(
    tools: dict[str, Callable[..., Any]], httpx_mock: HTTPXMock
) -> None:
    httpx_mock.add_response(
        url=f"{DATA}/maps.json",
        json=[
            {
                "id": "dam_battlegrounds",
                "name": {"en": "Dam Battlegrounds", "de": "Damm-Schlachtfelder"},
                "image": "https://cdn.test/dam.png",
            }
        ],
    )
    out = await tools["arc_list_maps"]()
    assert out["maps"] == [
        {
            "id": "dam_battlegrounds",
            "name": "Dam Battlegrounds",
            "image": "https://cdn.test/dam.png",
            "mapgenie_url": "https://mapgenie.io/arc-raiders/maps/dam-battlegrounds",
        }
    ]
    assert "RaidTheory" in out["source"]


# ── wiki ─────────────────────────────────────────────────────────────


async def test_search_wiki_strips_snippet_html(
    tools: dict[str, Callable[..., Any]], httpx_mock: HTTPXMock
) -> None:
    httpx_mock.add_response(
        url=(f"{WIKI}?action=query&list=search&srsearch=ferro&srlimit=5&format=json"),
        json={
            "query": {
                "searchinfo": {"totalhits": 39},
                "search": [
                    {
                        "title": "Ferro",
                        "snippet": 'by <span class="searchmatch">Ferro</span> &amp; co',
                        "timestamp": "2026-06-23T18:51:28Z",
                    }
                ],
            }
        },
    )
    out = await tools["arc_search_wiki"](query="ferro")
    assert out["results"][0]["snippet"] == "by Ferro & co"
    assert out["total"] == 39
    assert out["truncated"] is True


async def test_get_wiki_page_returns_text_and_wikitext(
    tools: dict[str, Callable[..., Any]], httpx_mock: HTTPXMock
) -> None:
    httpx_mock.add_response(
        url=(
            f"{WIKI}?action=query&prop=extracts&titles=Ferro&explaintext=1&redirects=1&format=json"
        ),
        json={
            "query": {
                "pages": {
                    "105": {"pageid": 105, "title": "Ferro", "extract": "The Ferro is a rifle."}
                }
            }
        },
    )
    httpx_mock.add_response(
        url=f"{WIKI}?action=parse&page=Ferro&prop=wikitext&redirects=1&format=json",
        json={"parse": {"title": "Ferro", "wikitext": {"*": "{{Infobox weapon|ammo=Heavy}}"}}},
    )
    out = await tools["arc_get_wiki_page"](title="Ferro")
    assert out["text"] == "The Ferro is a rifle."
    assert "Infobox weapon" in out["wikitext"]
    assert out["url"] == "https://arcraiders.wiki/wiki/Ferro"
    assert out["text_truncated"] is False
    assert out["license"] == "CC BY-SA 4.0"


async def test_get_wiki_page_missing_is_structured_error(
    tools: dict[str, Callable[..., Any]], httpx_mock: HTTPXMock
) -> None:
    httpx_mock.add_response(
        url=(
            f"{WIKI}?action=query&prop=extracts&titles=Nope&explaintext=1&redirects=1&format=json"
        ),
        json={"query": {"pages": {"-1": {"title": "Nope", "missing": ""}}}},
    )
    out = await tools["arc_get_wiki_page"](title="Nope")
    assert out["error"]["code"] == "arcraiders_wiki_page_not_found"


async def test_get_wiki_page_degrades_when_wikitext_fails(
    tools: dict[str, Callable[..., Any]], httpx_mock: HTTPXMock
) -> None:
    """A failed second (wikitext) request must not lose the extract."""
    httpx_mock.add_response(
        url=(
            f"{WIKI}?action=query&prop=extracts&titles=Ferro&explaintext=1&redirects=1&format=json"
        ),
        json={"query": {"pages": {"105": {"pageid": 105, "title": "Ferro", "extract": "Prose."}}}},
    )
    httpx_mock.add_response(
        url=f"{WIKI}?action=parse&page=Ferro&prop=wikitext&redirects=1&format=json",
        status_code=503,
    )
    out = await tools["arc_get_wiki_page"](title="Ferro")
    assert out["text"] == "Prose."
    assert out["wikitext"] == ""
