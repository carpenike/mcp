"""Server-wide tool-annotation contract.

claude.ai's permission UI groups a connector's tools by their MCP
annotations ("Read-only tools" vs "Write/delete tools") and dumps
unannotated tools into an individually-approved "Other tools" bucket.
This test pins the full, reviewed classification of every tool so a new
or edited tool can't silently ship unannotated — or worse, ship a
state-changing tool marked read-only (the hints are a security signal
first, display grouping second; see AGENTS.md).

The expected map is deliberately explicit, one row per tool: changing a
tool's safety posture must show up in this file's diff.
"""

from __future__ import annotations

import pytest
from mcp.server.fastmcp import FastMCP

from homelab_mcp.config import Settings
from homelab_mcp.tools import register_all

# name -> (readOnlyHint, destructiveHint, idempotentHint)
# openWorld is asserted separately: True for arc_* (public internet
# upstreams), False everywhere else (fixed internal homelab services).
EXPECTED: dict[str, tuple[bool, bool, bool]] = {
    # ── arc (public game data, pure lookups) ─────────────────────────
    "arc_search_items": (True, False, True),
    "arc_search_quests": (True, False, True),
    "arc_get_trader_stock": (True, False, True),
    "arc_check_item_keep": (True, False, True),
    "arc_plan_upgrades": (True, False, True),
    "arc_get_enemy": (True, False, True),
    "arc_who_drops": (True, False, True),
    "arc_compare_weapons": (True, False, True),
    "arc_get_event_schedule": (True, False, True),
    "arc_list_maps": (True, False, True),
    "arc_search_wiki": (True, False, True),
    "arc_get_wiki_page": (True, False, True),
    # ── cooklang ─────────────────────────────────────────────────────
    "cooklang_list_recipes": (True, False, True),
    "cooklang_get_recipe": (True, False, True),
    # create can clobber via overwrite=True and re-creates aren't repeat-safe
    "cooklang_create_recipe": (False, True, False),
    "cooklang_update_recipe": (False, True, True),
    "cooklang_delete_recipe": (False, True, True),
    "cooklang_search_federation": (True, False, True),
    "cooklang_build_shopping_list": (True, False, True),
    # ── homelab (gatus) ──────────────────────────────────────────────
    "homelab_list_status": (True, False, True),
    "homelab_get_endpoint_history": (True, False, True),
    # ── grocy ────────────────────────────────────────────────────────
    "grocy_health": (True, False, True),
    # find-or-create bootstrap tools are additive and repeat-safe
    "grocy_seed_defaults": (False, False, True),
    "grocy_ensure": (False, False, True),
    "grocy_find_products": (True, False, True),
    "grocy_attention": (True, False, True),
    # action='set' reconciles to an absolute amount (overwrite);
    # add/consume are not repeat-safe
    "grocy_stock_item": (False, True, False),
    "grocy_convert_units": (True, False, True),
    "grocy_product_card": (True, False, True),
    "grocy_consumption_history": (True, False, True),
    "grocy_stock_value": (True, False, True),
    "grocy_stock_by_location": (True, False, True),
    "grocy_set_unit_conversion": (False, True, True),
    # ── home assistant ───────────────────────────────────────────────
    "ha_health": (True, False, True),
    "ha_list_entities": (True, False, True),
    "ha_get_state": (True, False, True),
    "ha_get_history": (True, False, True),
    # physical actuation; toggles/scripts are not repeat-safe
    "ha_call_service": (False, True, False),
    "ha_list_automations": (True, False, True),
    "ha_get_automation": (True, False, True),
    "ha_upsert_automation": (False, True, True),
    # POST to HA's check endpoint, but purely a validation read
    "ha_check_config": (True, False, True),
}


@pytest.fixture(scope="module")
def served_tools(request: pytest.FixtureRequest) -> dict[str, object]:
    mp = pytest.MonkeyPatch()
    request.addfinalizer(mp.undo)
    mp.setenv("HOMELAB_MCP_OAUTH_REQUIRED", "false")
    mcp = FastMCP("test")
    register_all(mcp, Settings())
    import asyncio

    tools = asyncio.new_event_loop().run_until_complete(mcp.list_tools())
    return {t.name: t for t in tools}


def test_every_tool_is_annotated_as_reviewed(served_tools: dict[str, object]) -> None:
    assert set(served_tools) == set(EXPECTED), (
        "tool set drifted from the reviewed annotation map — "
        f"missing={set(EXPECTED) - set(served_tools)} "
        f"unreviewed={set(served_tools) - set(EXPECTED)}"
    )
    for name, (read_only, destructive, idempotent) in EXPECTED.items():
        ann = served_tools[name].annotations  # type: ignore[attr-defined]
        assert ann is not None, f"{name} has no annotations"
        assert ann.readOnlyHint is read_only, f"{name} readOnlyHint"
        assert ann.destructiveHint is destructive, f"{name} destructiveHint"
        assert ann.idempotentHint is idempotent, f"{name} idempotentHint"
        expected_open_world = name.startswith("arc_")
        assert ann.openWorldHint is expected_open_world, f"{name} openWorldHint"


def test_no_state_changing_tool_claims_read_only() -> None:
    """Redundant belt-and-braces: the writers must never regress to read-only."""
    writers = [n for n, (ro, _, _) in EXPECTED.items() if not ro]
    assert "ha_call_service" in writers
    assert "cooklang_delete_recipe" in writers
    assert "grocy_stock_item" in writers
