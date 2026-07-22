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
WIKI = "https://wiki.test/w/api.php"

pytestmark = pytest.mark.httpx_mock(assert_all_responses_were_requested=False)


class CapturingMCP:
    """Collects tools registered via @mcp.tool(name=...) so tests can call them."""

    def __init__(self) -> None:
        self.tools: dict[str, Callable[..., Any]] = {}

    def tool(self, *, name: str, description: str = "") -> Callable[..., Any]:
        def deco(fn: Callable[..., Any]) -> Callable[..., Any]:
            self.tools[name] = fn
            return fn

        return deco


@pytest.fixture
def tools(monkeypatch: pytest.MonkeyPatch) -> dict[str, Callable[..., Any]]:
    monkeypatch.setenv("HOMELAB_MCP_OAUTH_REQUIRED", "false")
    monkeypatch.setenv("HOMELAB_MCP_ARCRAIDERS_METAFORGE_BASE_URL", METAFORGE)
    monkeypatch.setenv("HOMELAB_MCP_ARCRAIDERS_DATA_BASE_URL", DATA)
    monkeypatch.setenv("HOMELAB_MCP_ARCRAIDERS_WIKI_API_URL", WIKI)
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
