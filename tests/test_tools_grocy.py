"""Grocy stock/inventory tool tests.

Layers:
  * Pure-helper tests (no I/O).
  * A stateful in-memory FakeGrocy that mirrors the master-data + stock
    wire closely enough to exercise the find-or-create contract, the
    seed/bootstrap path, and the keystone walkthrough tool end-to-end —
    including the nine-step cold-instance acceptance loop from the handoff.
"""

from __future__ import annotations

import json
import re
from datetime import datetime, timedelta
from typing import Any
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest

from homelab_mcp.config import Settings
from homelab_mcp.tools.grocy import _compact, _enc, _norm, register

API_KEY = "test-key-123"
BASE = "https://grocy.test"

pytestmark = pytest.mark.httpx_mock(assert_all_responses_were_requested=False)


# ─────────────────────────────────────────────────────────────────────────
# Pure helpers
# ─────────────────────────────────────────────────────────────────────────
def test_compact_drops_none() -> None:
    assert _compact({"a": 1, "b": None, "c": 0, "d": False}) == {"a": 1, "c": 0, "d": False}


@pytest.mark.parametrize(
    ("raw", "expected"),
    [("012345", "012345"), ("../etc/passwd", "..%2Fetc%2Fpasswd"), ("a/b", "a%2Fb")],
)
def test_enc_encodes_path_segment(raw: str, expected: str) -> None:
    assert _enc(raw) == expected


def test_norm_trims_and_lowercases() -> None:
    assert _norm("  Ribeye ") == "ribeye"
    assert _norm(None) == ""


# ─────────────────────────────────────────────────────────────────────────
# Stateful fake Grocy
# ─────────────────────────────────────────────────────────────────────────
class FakeGrocy:
    """Minimal stateful Grocy: master data + stock actions over an in-memory store."""

    def __init__(self) -> None:
        self.locations: list[dict[str, Any]] = []
        self.units: list[dict[str, Any]] = []
        self.products: list[dict[str, Any]] = []
        self.stores: list[dict[str, Any]] = []
        self.userfield_defs: list[dict[str, Any]] = []
        self.userfield_values: dict[tuple[str, int], dict[str, Any]] = {}
        self.stock: dict[int, float] = {}  # product_id -> amount
        self.requests: list[httpx.Request] = []
        # When True, the server-side `query[]` (`~` LIKE) filter returns nothing
        # — simulating the live Grocy path where the filter failed to return an
        # existing row. Exact master-data resolution must not depend on it.
        self.filter_blind = False

    # -- helpers ---------------------------------------------------------
    @staticmethod
    def _like(query: str | None) -> str | None:
        """Extract the substring value from a `name~value` query[] filter."""
        if not query:
            return None
        m = re.match(r"name~(.*)", query)
        return m.group(1) if m else None

    def _coll(self, entity: str) -> list[dict[str, Any]]:
        return {
            "locations": self.locations,
            "quantity_units": self.units,
            "products": self.products,
            "shopping_locations": self.stores,
            "userfields": self.userfield_defs,
        }[entity]

    def _detail(self, pid: int) -> dict[str, Any] | None:
        prod = next((p for p in self.products if p["id"] == pid), None)
        if prod is None:
            return None
        unit = next((u for u in self.units if u["id"] == prod.get("qu_id_stock")), {})
        loc = next((c for c in self.locations if c["id"] == prod.get("location_id")), {})
        return {
            "product": prod,
            "stock_amount": self.stock.get(pid, 0),
            "quantity_unit_stock": {"name": unit.get("name")},
            "location": {"name": loc.get("name")},
            "next_due_date": "2026-12-01",
        }

    # -- dispatch --------------------------------------------------------
    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if request.headers.get("GROCY-API-KEY") != API_KEY:
            return httpx.Response(400, json={"error_message": "Invalid API key"})

        url = urlsplit(str(request.url))
        path, method = url.path, request.method
        qs = parse_qs(url.query)
        body = json.loads(request.content) if request.content else {}

        if path == "/system/info":
            return httpx.Response(200, json={"grocy_version": {"Version": "4.0.0"}})

        if path == "/stock" and method == "GET":
            out = [
                {
                    "product_id": p["id"],
                    "amount": self.stock.get(p["id"], 0),
                    "best_before_date": "2026-12-01",
                    "product": {"name": p["name"]},
                }
                for p in self.products
                if self.stock.get(p["id"], 0) > 0
            ]
            return httpx.Response(200, json=out)

        if path == "/stock/volatile" and method == "GET":
            due = [
                {"product_id": p["id"], "amount": self.stock.get(p["id"], 0)}
                for p in self.products
                if self.stock.get(p["id"], 0) > 0
            ]
            return httpx.Response(
                200,
                json={
                    "due_products": due,
                    "overdue_products": [],
                    "expired_products": [],
                    "missing_products": [],
                },
            )

        # /objects/{entity}
        m = re.match(r"^/objects/(\w+)$", path)
        if m:
            entity = m.group(1)
            if method == "GET":
                like = self._like(qs.get("query[]", [None])[0])
                items = self._coll(entity)
                if like is not None:
                    if self.filter_blind:
                        return httpx.Response(200, json=[])
                    items = [i for i in items if like.lower() in str(i["name"]).lower()]
                return httpx.Response(200, json=items)
            if method == "POST":
                coll = self._coll(entity)
                new_id = len(coll) + 1
                rec = {**body, "id": new_id}
                coll.append(rec)
                return httpx.Response(200, json={"created_object_id": new_id})

        m = re.match(r"^/objects/products/(\d+)$", path)
        if m and method == "GET":
            pid = int(m.group(1))
            prod = next((p for p in self.products if p["id"] == pid), None)
            if prod is None:
                return httpx.Response(404, json={"error_message": "Not existing product"})
            return httpx.Response(200, json=prod)

        m = re.match(r"^/userfields/(\w+)/(\d+)$", path)
        if m:
            key = (m.group(1), int(m.group(2)))
            if method == "GET":
                return httpx.Response(200, json=self.userfield_values.get(key, {}))
            if method == "PUT":
                self.userfield_values.setdefault(key, {}).update(body)
                return httpx.Response(204)

        m = re.match(r"^/stock/products/(\d+)$", path)
        if m and method == "GET":
            detail = self._detail(int(m.group(1)))
            if detail is None:
                return httpx.Response(400, json={"error_message": "Not existing product"})
            return httpx.Response(200, json=detail)

        m = re.match(r"^/stock/products/(\d+)/(inventory|add|consume|open)$", path)
        if m and method == "POST":
            pid, op = int(m.group(1)), m.group(2)
            cur = self.stock.get(pid, 0)
            if op == "inventory":
                self.stock[pid] = float(body["new_amount"])
            elif op == "add":
                self.stock[pid] = cur + float(body["amount"])
            elif op == "consume":
                want = float(body["amount"])
                if want > cur:
                    return httpx.Response(
                        400, json={"error_message": "Amount to be consumed > stock amount"}
                    )
                self.stock[pid] = cur - want
            return httpx.Response(200, json=[{"id": 1}])

        return httpx.Response(404, json={"error_message": f"unhandled {method} {path}"})


class CapturingMCP:
    def __init__(self) -> None:
        self.tools: dict[str, Any] = {}

    def tool(self, *, name: str, description: str) -> Any:
        def deco(fn: Any) -> Any:
            self.tools[name] = fn
            return fn

        return deco


def _settings(api_key: str = API_KEY) -> Settings:
    return Settings(oauth_required=False, grocy_base_url=BASE, grocy_api_key=api_key)


@pytest.fixture
def tools() -> dict[str, Any]:
    mcp = CapturingMCP()
    register(mcp, _settings())  # type: ignore[arg-type]
    return mcp.tools


@pytest.fixture
def fake(httpx_mock: Any) -> FakeGrocy:
    server = FakeGrocy()
    httpx_mock.add_callback(server, is_reusable=True)
    return server


# ─────────────────────────────────────────────────────────────────────────
# Diagnostics / bootstrap
# ─────────────────────────────────────────────────────────────────────────
@pytest.mark.asyncio
async def test_health_reports_version(tools: dict[str, Any], fake: FakeGrocy) -> None:
    res = await tools["grocy_health"]()
    assert res["ok"] is True
    assert res["grocy_version"] == "4.0.0"


@pytest.mark.parametrize(
    "info",
    [
        {"grocy_version": {"Version": "4.6.0"}},
        {"grocy_version": {"version": "4.6.0"}},
        {"grocy_version": "4.6.0"},
        {"version": "4.6.0"},
    ],
)
@pytest.mark.asyncio
async def test_health_version_shapes(
    tools: dict[str, Any], httpx_mock: Any, info: dict[str, Any]
) -> None:
    """The version shape has varied across Grocy releases; read it robustly so
    grocy_health never reports 'None' against a reachable instance."""
    httpx_mock.add_response(url=f"{BASE}/api/system/info", json=info)
    res = await tools["grocy_health"]()
    assert res["grocy_version"] == "4.6.0"


@pytest.mark.asyncio
async def test_health_uses_api_route_then_falls_back(
    tools: dict[str, Any], httpx_mock: Any
) -> None:
    """If /api/system/info is wrong for the deployment (404/redirect), fall back
    to the legacy /system/info path rather than reporting null."""
    httpx_mock.add_response(url=f"{BASE}/api/system/info", status_code=404, json={})
    httpx_mock.add_response(url=f"{BASE}/system/info", json={"grocy_version": {"Version": "4.6.0"}})
    res = await tools["grocy_health"]()
    assert res["grocy_version"] == "4.6.0"


@pytest.mark.asyncio
async def test_health_unknown_shape_echoes_raw(tools: dict[str, Any], httpx_mock: Any) -> None:
    """An unrecognized system-info shape must still report ok=true AND echo the
    raw status + body, so the actual version field is visible without logs."""
    payload = {"some_new_version_field": "4.6.0", "php_version": "8.3"}
    httpx_mock.add_response(url=f"{BASE}/api/system/info", json=payload)
    res = await tools["grocy_health"]()
    assert res["ok"] is True
    assert res["grocy_version"] is None
    assert res["raw_system_info"]["path"] == "/api/system/info"
    assert res["raw_system_info"]["status"] == 200
    assert res["raw_system_info"]["json"] == payload


@pytest.mark.asyncio
async def test_health_redirect_surfaces_raw(tools: dict[str, Any], httpx_mock: Any) -> None:
    """The actual live failure: the web route 302→login (HTML, no body). With the
    API route also unavailable here, health stays ok and surfaces the redirect."""
    httpx_mock.add_response(
        url=f"{BASE}/api/system/info",
        status_code=302,
        headers={"content-type": "text/html"},
        content=b"",
    )
    httpx_mock.add_response(
        url=f"{BASE}/system/info",
        status_code=302,
        headers={"content-type": "text/html"},
        content=b"",
    )
    res = await tools["grocy_health"]()
    assert res["ok"] is True
    assert res["grocy_version"] is None
    assert res["raw_system_info"]["status"] == 302
    assert res["raw_system_info"]["content_type"] == "text/html"


@pytest.mark.asyncio
async def test_health_empty_body_reports_raw_status(tools: dict[str, Any], httpx_mock: Any) -> None:
    """A 2xx with NO body still reports ok and surfaces the raw status/path."""
    httpx_mock.add_response(url=f"{BASE}/api/system/info", status_code=200, content=b"")
    res = await tools["grocy_health"]()
    assert res["ok"] is True
    assert res["grocy_version"] is None
    assert res["raw_system_info"]["path"] == "/api/system/info"
    assert res["raw_system_info"]["status"] == 200
    assert res["raw_system_info"]["json"] is None
    assert res["raw_system_info"]["body_excerpt"] == ""


@pytest.mark.asyncio
async def test_seed_defaults_is_idempotent(tools: dict[str, Any], fake: FakeGrocy) -> None:
    first = await tools["grocy_seed_defaults"]()
    assert first["created_count"] == 8  # 4 locations + 4 units
    assert first["existing_count"] == 0
    second = await tools["grocy_seed_defaults"]()
    assert second["created_count"] == 0
    assert second["existing_count"] == 8


@pytest.mark.asyncio
async def test_ensure_location_and_unit(tools: dict[str, Any], fake: FakeGrocy) -> None:
    a = await tools["grocy_ensure_location"](name="Chest Freezer")
    assert a["created"] is True
    b = await tools["grocy_ensure_location"](name="chest freezer")  # case-insensitive
    assert b["created"] is False
    assert b["id"] == a["id"]
    u = await tools["grocy_ensure_unit"](name="count")
    assert u["created"] is True


# ─────────────────────────────────────────────────────────────────────────
# Nine-step cold-instance acceptance loop (handoff §10)
# ─────────────────────────────────────────────────────────────────────────
@pytest.mark.asyncio
async def test_cold_instance_acceptance_loop(tools: dict[str, Any], fake: FakeGrocy) -> None:
    # 2. seed
    await tools["grocy_seed_defaults"]()
    # 3. empty stock
    assert (await tools["grocy_list_stock"]())["count"] == 0
    # 4. create + set ribeye to 2
    r = await tools["grocy_stock_item"](
        name="ribeye",
        amount=2,
        location="Chest Freezer",
        action="set",
        unit="count",
        best_before="2026-12-01",
    )
    assert r["product"]["created"] is True
    assert r["resulting_amount_on_hand"] == 2
    # 5. find it
    hits = await tools["grocy_find_products"](query="ribeye")
    assert hits["count"] == 1
    assert hits["products"][0]["amount_on_hand"] == 2
    # 6. "Ribeye" again resolves to the SAME product (no duplicate)
    r2 = await tools["grocy_stock_item"](name="Ribeye", amount=1, location="Chest Freezer")
    assert r2["product"]["created"] is False
    assert len(fake.products) == 1
    # 7. consume 1 → on hand 1 (after step 6 set it to 1, consume 1 → 0; adjust)
    #    step 6 set to 1, so consume 1 leaves 0; verify the action plumbs through
    r3 = await tools["grocy_stock_item"](
        name="ribeye", amount=1, action="consume", location="Chest Freezer"
    )
    assert r3["resulting_amount_on_hand"] == 0
    # 8. expiring feed reachable + structured
    exp = await tools["grocy_expiring"](days=365)
    assert set(exp) >= {"due_products", "overdue_products", "expired_products", "missing_products"}
    # 9. still exactly one product
    assert len(fake.products) == 1


# ─────────────────────────────────────────────────────────────────────────
# Find-or-create contract
# ─────────────────────────────────────────────────────────────────────────
@pytest.mark.asyncio
async def test_near_match_triggers_disambiguation(tools: dict[str, Any], fake: FakeGrocy) -> None:
    await tools["grocy_seed_defaults"]()
    # Seed one product whose name merely CONTAINS "ribeye".
    await tools["grocy_stock_item"](
        name="Ribeye Steak", amount=1, location="Chest Freezer", unit="count"
    )
    assert len(fake.products) == 1
    res = await tools["grocy_stock_item"](
        name="ribeye", amount=1, location="Chest Freezer", create_if_missing=True
    )
    assert res["error"]["code"] == "needs_disambiguation"
    assert res["candidates"][0]["name"] == "Ribeye Steak"
    assert len(fake.products) == 1  # nothing created


@pytest.mark.asyncio
async def test_product_id_bypasses_resolution(tools: dict[str, Any], fake: FakeGrocy) -> None:
    await tools["grocy_seed_defaults"]()
    created = await tools["grocy_stock_item"](
        name="chicken", amount=3, location="Chest Freezer", unit="count"
    )
    pid = created["product"]["id"]
    res = await tools["grocy_stock_item"](
        product_id=pid, amount=5, location="Chest Freezer", action="set"
    )
    assert res["product"]["id"] == pid
    assert res["resulting_amount_on_hand"] == 5


# ─────────────────────────────────────────────────────────────────────────
# Guard rails / structured errors
# ─────────────────────────────────────────────────────────────────────────
@pytest.mark.asyncio
async def test_location_not_found(tools: dict[str, Any], fake: FakeGrocy) -> None:
    res = await tools["grocy_stock_item"](name="ribeye", amount=1, location="Nowhere")
    assert res["error"]["code"] == "location_not_found"


@pytest.mark.asyncio
async def test_unit_not_found_on_create(tools: dict[str, Any], fake: FakeGrocy) -> None:
    await tools["grocy_ensure_location"](name="Chest Freezer")  # location exists, unit does not
    res = await tools["grocy_stock_item"](
        name="ribeye", amount=1, location="Chest Freezer", unit="ton"
    )
    assert res["error"]["code"] == "unit_not_found"


@pytest.mark.asyncio
async def test_amount_invalid(tools: dict[str, Any], fake: FakeGrocy) -> None:
    res = await tools["grocy_stock_item"](name="ribeye", amount=0, location="Chest Freezer")
    assert res["error"]["code"] == "amount_invalid"


@pytest.mark.asyncio
async def test_unknown_action(tools: dict[str, Any], fake: FakeGrocy) -> None:
    res = await tools["grocy_stock_item"](
        name="ribeye", amount=1, location="Chest Freezer", action="frobnicate"
    )
    assert res["error"]["code"] == "amount_invalid"


# ─────────────────────────────────────────────────────────────────────────
# Price capture + store tracking (handoff #4)
# ─────────────────────────────────────────────────────────────────────────
def _last_add_body(fake: FakeGrocy) -> dict[str, Any] | None:
    for r in reversed(fake.requests):
        if r.url.path.endswith("/add"):
            return json.loads(r.content)  # type: ignore[no-any-return]
    return None


@pytest.mark.asyncio
async def test_add_with_total_price_creates_store(tools: dict[str, Any], fake: FakeGrocy) -> None:
    await tools["grocy_seed_defaults"]()
    res = await tools["grocy_stock_item"](
        name="brisket",
        amount=14.2,
        unit="lb",
        location="Chest Freezer",
        action="add",
        total_price=45,
        store="Costco",
        best_before="2026-12-19",
    )
    assert res["resulting_amount_on_hand"] == 14.2
    assert round(res["unit_price"], 2) == 3.17
    assert res["total_price"] == 45.0
    assert res["store"] == "Costco"
    assert any(s["name"] == "Costco" for s in fake.stores)  # auto-created
    body = _last_add_body(fake)
    assert body is not None
    assert round(body["price"], 2) == 3.17
    assert body["shopping_location_id"] == fake.stores[0]["id"]
    assert "purchased_date" not in body  # not passed in this call


@pytest.mark.asyncio
async def test_priced_add_new_product_resolves_location_when_filter_blind(
    tools: dict[str, Any], fake: FakeGrocy
) -> None:
    """Regression for the live bug: a NEW product booked via a priced add failed
    with location_not_found even though the location existed. Root cause was
    exact resolution depending on the server-side `~` filter; here that filter
    returns nothing, yet location/unit must still resolve (unfiltered) and the
    product must be created + booked in one call.
    """
    fake.filter_blind = True
    await tools["grocy_seed_defaults"]()
    res = await tools["grocy_stock_item"](
        name="ground beef",
        amount=5,
        unit="lb",
        location="Chest Freezer",
        action="add",
        total_price=25,
    )
    assert "error" not in res, res
    assert res["product"]["created"] is True
    assert res["resulting_amount_on_hand"] == 5.0
    assert round(res["unit_price"], 2) == 5.0


@pytest.mark.asyncio
async def test_create_within_priced_add_sets_price_unit(
    tools: dict[str, Any], fake: FakeGrocy
) -> None:
    """Regression: a NEW product booked via a priced add (no store, no
    best_before — the reported repro) must succeed, and the product-create body
    must carry the price/consume quantity units. A priced add resolves
    qu_id_price; without it Grocy rejects the add while a non-priced set works.
    """
    await tools["grocy_seed_defaults"]()
    res = await tools["grocy_stock_item"](
        name="newcut",
        amount=2,
        unit="lb",
        location="Chest Freezer",
        action="add",
        total_price=45,
    )
    assert "error" not in res, res
    assert res["product"]["created"] is True
    assert res["resulting_amount_on_hand"] == 2.0
    create = next(
        json.loads(r.content)
        for r in fake.requests
        if r.method == "POST" and r.url.path == "/objects/products"
    )
    assert create["qu_id_price"] == create["qu_id_stock"]
    assert create["qu_id_consume"] == create["qu_id_stock"]


@pytest.mark.asyncio
async def test_add_with_unit_price(tools: dict[str, Any], fake: FakeGrocy) -> None:
    await tools["grocy_seed_defaults"]()
    res = await tools["grocy_stock_item"](
        name="ribeye",
        amount=2,
        unit="count",
        location="Chest Freezer",
        action="add",
        unit_price=3.5,
    )
    assert res["unit_price"] == 3.5
    assert res["total_price"] == 7.0
    assert round(_last_add_body(fake)["price"], 2) == 3.5  # type: ignore[index]


@pytest.mark.asyncio
async def test_price_ambiguous(tools: dict[str, Any], fake: FakeGrocy) -> None:
    res = await tools["grocy_stock_item"](
        name="x", amount=1, location="Chest Freezer", action="add", total_price=10, unit_price=3
    )
    assert res["error"]["code"] == "price_ambiguous"


@pytest.mark.asyncio
async def test_price_invalid_negative(tools: dict[str, Any], fake: FakeGrocy) -> None:
    res = await tools["grocy_stock_item"](
        name="x", amount=1, location="Chest Freezer", action="add", total_price=-5
    )
    assert res["error"]["code"] == "price_invalid"


@pytest.mark.asyncio
async def test_price_rejected_on_non_add(tools: dict[str, Any], fake: FakeGrocy) -> None:
    res = await tools["grocy_stock_item"](
        name="x", amount=1, location="Chest Freezer", action="set", total_price=5
    )
    assert res["error"]["code"] == "price_invalid"


@pytest.mark.asyncio
async def test_add_without_price_is_backward_compatible(
    tools: dict[str, Any], fake: FakeGrocy
) -> None:
    await tools["grocy_seed_defaults"]()
    res = await tools["grocy_stock_item"](
        name="eggs", amount=12, unit="count", location="Kitchen Fridge", action="add"
    )
    assert res["unit_price"] is None
    assert res["store"] is None
    body = _last_add_body(fake)
    assert body is not None
    assert "price" not in body  # None values are dropped — no price recorded
    assert "shopping_location_id" not in body


@pytest.mark.asyncio
async def test_ensure_store_idempotent(tools: dict[str, Any], fake: FakeGrocy) -> None:
    a = await tools["grocy_ensure_store"](name="Costco")
    assert a["created"] is True
    assert a["address"] is None
    assert a["updated"] is False
    b = await tools["grocy_ensure_store"](name="costco")  # case-insensitive
    assert b["created"] is False
    assert b["id"] == a["id"]


@pytest.mark.asyncio
async def test_ensure_store_address_on_create(tools: dict[str, Any], fake: FakeGrocy) -> None:
    addr = "6316 Mount Phillip Road, Frederick, MD 21703"
    res = await tools["grocy_ensure_store"](name="Stone Pillar Farm", address=addr)
    assert res["created"] is True
    assert res["address"] == addr
    # The address lives in a userfield, NOT the store's description column.
    store = next(s for s in fake.stores if s["name"] == "Stone Pillar Farm")
    assert "description" not in store
    assert fake.userfield_values[("shopping_locations", store["id"])]["address"] == addr


@pytest.mark.asyncio
async def test_ensure_store_address_not_clobbered(tools: dict[str, Any], fake: FakeGrocy) -> None:
    addr = "123 Farm Rd"
    await tools["grocy_ensure_store"](name="Farm", address=addr)
    # Re-run with no address: must not wipe the existing one.
    res = await tools["grocy_ensure_store"](name="farm")
    assert res["created"] is False
    assert res["updated"] is False
    assert res["address"] == addr


@pytest.mark.asyncio
async def test_ensure_store_backfills_existing(tools: dict[str, Any], fake: FakeGrocy) -> None:
    first = await tools["grocy_ensure_store"](name="Market")  # no address
    assert first["created"] is True
    assert first["address"] is None
    res = await tools["grocy_ensure_store"](name="Market", address="1 Main St")
    assert res["created"] is False
    assert res["updated"] is True
    assert res["address"] == "1 Main St"


@pytest.mark.asyncio
async def test_ensure_store_updates_changed_address(tools: dict[str, Any], fake: FakeGrocy) -> None:
    await tools["grocy_ensure_store"](name="Shop", address="old")
    res = await tools["grocy_ensure_store"](name="Shop", address="new")
    assert res["updated"] is True
    assert res["address"] == "new"
    # Same address again → no update.
    again = await tools["grocy_ensure_store"](name="Shop", address="new")
    assert again["updated"] is False


@pytest.mark.asyncio
async def test_consume_over_stock_surfaces_http_error(
    tools: dict[str, Any], fake: FakeGrocy
) -> None:
    await tools["grocy_seed_defaults"]()
    await tools["grocy_stock_item"](name="ribeye", amount=1, location="Chest Freezer", unit="count")
    res = await tools["grocy_stock_item"](
        name="ribeye", amount=99, location="Chest Freezer", action="consume"
    )
    assert res["error"]["code"] == "grocy_http_400"
    assert "stock amount" in res["error"]["message"].lower()


@pytest.mark.asyncio
async def test_missing_api_key_short_circuits(httpx_mock: Any) -> None:
    mcp = CapturingMCP()
    register(mcp, _settings(api_key=""))  # type: ignore[arg-type]
    res = await mcp.tools["grocy_list_stock"]()
    assert res["error"]["code"] == "grocy_unreachable"
    assert not httpx_mock.get_requests()


# ─────────────────────────────────────────────────────────────────────────
# Primitives
# ─────────────────────────────────────────────────────────────────────────
@pytest.mark.asyncio
async def test_consume_primitive_requires_one_identifier(tools: dict[str, Any]) -> None:
    both = await tools["grocy_consume_product"](product_id=1, barcode="x")
    neither = await tools["grocy_consume_product"]()
    assert both["error"]["code"] == "amount_invalid"
    assert neither["error"]["code"] == "amount_invalid"


@pytest.mark.asyncio
async def test_open_primitive_by_barcode_is_encoded(tools: dict[str, Any], fake: FakeGrocy) -> None:
    await tools["grocy_open_product"](barcode="../x", amount=1)
    path = urlsplit(str(fake.requests[-1].url)).path
    assert path == "/stock/products/by-barcode/..%2Fx/open"


# ─────────────────────────────────────────────────────────────────────────
# Enrichment read tools — seeded instance (handoff #2 acceptance loop)
# ─────────────────────────────────────────────────────────────────────────
def _ago(days: int) -> str:
    return (datetime.now() - timedelta(days=days)).strftime("%Y-%m-%d %H:%M:%S")


class SeededGrocy:
    """Read-only fake pre-seeded with the handoff #2 acceptance preconditions.

    One in-stock, priced product ('Ribeye') with a product-specific conversion
    (1 pack = 4 lb), a global conversion (1 lb = 16 oz), a below-minimum level,
    and a small transaction log. Records every request so read-only-ness can be
    asserted.
    """

    def __init__(self) -> None:
        self.requests: list[httpx.Request] = []
        self.collections: dict[str, list[dict[str, Any]]] = {
            "products": [
                {
                    "id": 1,
                    "name": "Ribeye",
                    "location_id": 1,
                    "qu_id_stock": 1,
                    "qu_id_purchase": 1,
                    "min_stock_amount": 5,
                    "product_group_id": 1,
                },
                {"id": 2, "name": "Ribeye Tip", "location_id": 1, "qu_id_stock": 1},
            ],
            "quantity_units": [
                {"id": 1, "name": "count"},
                {"id": 2, "name": "pack"},
                {"id": 3, "name": "lb"},
                {"id": 4, "name": "oz"},
            ],
            "quantity_unit_conversions": [
                {"id": 1, "product_id": 1, "from_qu_id": 2, "to_qu_id": 3, "factor": 4},
                {"id": 2, "product_id": None, "from_qu_id": 3, "to_qu_id": 4, "factor": 16},
            ],
            "locations": [{"id": 1, "name": "Chest Freezer"}],
            "product_groups": [{"id": 1, "name": "Meat"}],
            "stock": [
                {
                    "id": 1,
                    "product_id": 1,
                    "amount": 2,
                    "price": 10.0,
                    "location_id": 1,
                    "best_before_date": "2026-12-01",
                }
            ],
            "stock_log": [
                {
                    "id": 1,
                    "product_id": 1,
                    "transaction_type": "purchase",
                    "amount": 4,
                    "purchased_date": _ago(40)[:10],
                    "row_created_timestamp": _ago(40),
                },
                {
                    "id": 2,
                    "product_id": 1,
                    "transaction_type": "consume",
                    "amount": 1,
                    "spoiled": False,
                    "used_date": _ago(20)[:10],
                    "row_created_timestamp": _ago(20),
                },
                {
                    "id": 3,
                    "product_id": 1,
                    "transaction_type": "consume",
                    "amount": 1,
                    "spoiled": True,
                    "row_created_timestamp": _ago(10),
                },
            ],
        }

    @staticmethod
    def _cond(item: dict[str, Any], cond: str) -> bool:
        if "~" in cond:
            f, v = cond.split("~", 1)
            return v.lower() in str(item.get(f) or "").lower()
        if "=" in cond:
            f, v = cond.split("=", 1)
            cur = item.get(f)
            return str(cur if cur is not None else "") == v
        return True

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if request.headers.get("GROCY-API-KEY") != API_KEY:
            return httpx.Response(400, json={"error_message": "Invalid API key"})

        url = urlsplit(str(request.url))
        path = url.path
        conds = parse_qs(url.query).get("query[]", [])

        if path == "/system/info":
            return httpx.Response(200, json={"grocy_version": {"Version": "4.6.0"}})
        if path == "/system/config":
            return httpx.Response(200, json={"CURRENCY": "USD"})

        if path == "/stock/volatile":
            return httpx.Response(
                200,
                json={
                    "due_products": [
                        {"product": {"name": "Ribeye"}, "best_before_date": "2026-12-01"}
                    ],
                    "overdue_products": [],
                    "expired_products": [],
                    "missing_products": [
                        {"id": 1, "name": "Ribeye", "amount_missing": 3, "is_partly_in_stock": 1}
                    ],
                },
            )

        m = re.match(r"^/stock/products/(\d+)/locations$", path)
        if m:
            return httpx.Response(
                200,
                json=[{"location_id": 1, "location_name": "Chest Freezer", "amount": 2}],
            )

        m = re.match(r"^/stock/products/(\d+)$", path)
        if m:
            prod = next(
                (p for p in self.collections["products"] if p["id"] == int(m.group(1))), None
            )
            if prod is None:
                return httpx.Response(400, json={"error_message": "Not existing product"})
            return httpx.Response(
                200,
                json={
                    "product": prod,
                    "stock_amount": 2,
                    "stock_amount_opened": 0,
                    "quantity_unit_stock": {"name": "count"},
                    "location": {"name": "Chest Freezer"},
                    "default_location": {"name": "Chest Freezer"},
                    "next_due_date": "2026-12-01",
                    "average_shelf_life_days": 120,
                    "last_purchased": "2026-05-01",
                    "last_price": 10.0,
                    "avg_price": 9.5,
                },
            )

        m = re.match(r"^/objects/(\w+)/(\d+)$", path)
        if m:
            coll = self.collections.setdefault(m.group(1), [])
            rec = next((i for i in coll if i.get("id") == int(m.group(2))), None)
            if request.method == "PUT":
                if rec is None:
                    return httpx.Response(400, json={"error_message": "Not found"})
                rec.update(json.loads(request.content) if request.content else {})
                return httpx.Response(204)
            if rec is None:
                return httpx.Response(404, json={"error_message": "Not found"})
            return httpx.Response(200, json=rec)

        m = re.match(r"^/objects/(\w+)$", path)
        if m:
            coll = self.collections.setdefault(m.group(1), [])
            if request.method == "POST":
                body = json.loads(request.content) if request.content else {}
                new_id = max((i["id"] for i in coll), default=0) + 1
                coll.append({**body, "id": new_id})
                return httpx.Response(200, json={"created_object_id": new_id})
            items = list(coll)
            for c in conds:
                items = [i for i in items if self._cond(i, c)]
            return httpx.Response(200, json=items)

        return httpx.Response(404, json={"error_message": f"unhandled {path}"})


@pytest.fixture
def seeded(httpx_mock: Any) -> SeededGrocy:
    server = SeededGrocy()
    httpx_mock.add_callback(server, is_reusable=True)
    return server


@pytest.mark.asyncio
async def test_convert_units_product_specific(tools: dict[str, Any], seeded: SeededGrocy) -> None:
    res = await tools["grocy_convert_units"](
        product="ribeye", amount=1, from_unit="pack", to_unit="lb"
    )
    assert res["amount_out"] == 4
    assert res["conversion_source"] == "product_specific"


@pytest.mark.asyncio
async def test_convert_units_global(tools: dict[str, Any], seeded: SeededGrocy) -> None:
    res = await tools["grocy_convert_units"](
        product="ribeye", amount=2, from_unit="lb", to_unit="oz"
    )
    assert res["amount_out"] == 32
    assert res["conversion_source"] == "global"


@pytest.mark.asyncio
async def test_convert_units_no_path(tools: dict[str, Any], seeded: SeededGrocy) -> None:
    res = await tools["grocy_convert_units"](
        product="ribeye", amount=1, from_unit="count", to_unit="lb"
    )
    assert res["error"]["code"] == "no_conversion_path"


@pytest.mark.asyncio
async def test_convert_units_identity(tools: dict[str, Any], seeded: SeededGrocy) -> None:
    res = await tools["grocy_convert_units"](
        product="ribeye", amount=3, from_unit="lb", to_unit="lb"
    )
    assert res["amount_out"] == 3
    assert res["conversion_source"] == "identity"


@pytest.mark.asyncio
async def test_convert_units_disambiguation(tools: dict[str, Any], seeded: SeededGrocy) -> None:
    res = await tools["grocy_convert_units"](
        product="ribeye t", amount=1, from_unit="lb", to_unit="oz"
    )
    assert res["error"]["code"] == "needs_disambiguation"
    assert res["candidates"]


@pytest.mark.asyncio
async def test_product_card(tools: dict[str, Any], seeded: SeededGrocy) -> None:
    card = await tools["grocy_product_card"](product="ribeye")
    assert card["on_hand"] == 2
    assert card["last_price"] == 10.0
    assert card["next_due_date"] == "2026-12-01"
    assert card["default_location"] == "Chest Freezer"
    assert card["product_group"] == "Meat"
    assert card["below_minimum"] is True
    assert card["locations"] == [{"location": "Chest Freezer", "amount": 2}]


@pytest.mark.asyncio
async def test_consumption_history(tools: dict[str, Any], seeded: SeededGrocy) -> None:
    res = await tools["grocy_consumption_history"](product="ribeye", days=90)
    assert res["purchased_total"] == 4
    assert res["consumed_total"] == 1
    assert res["spoiled_total"] == 1
    assert res["transactions_count"] == 3
    assert res["consume_rate_per_week"] > 0


@pytest.mark.asyncio
async def test_stock_value_by_location(tools: dict[str, Any], seeded: SeededGrocy) -> None:
    res = await tools["grocy_stock_value"](by_location=True)
    assert res["total_value"] == 20.0
    assert res["currency"] == "USD"
    assert res["by_location"] == [{"location": "Chest Freezer", "value": 20.0}]
    assert res["by_product_top"][0] == {"product": "Ribeye", "value": 20.0}


@pytest.mark.asyncio
async def test_restock_suggestions(tools: dict[str, Any], seeded: SeededGrocy) -> None:
    res = await tools["grocy_restock_suggestions"]()
    assert res["below_minimum"] == [
        {
            "product": "Ribeye",
            "on_hand": 2,
            "min_stock": 5,
            "shortfall": 3,
            "default_location": "Chest Freezer",
        }
    ]


@pytest.mark.asyncio
async def test_stock_by_location_scoped(tools: dict[str, Any], seeded: SeededGrocy) -> None:
    res = await tools["grocy_stock_by_location"](location="Chest Freezer")
    assert res["locations"] == [
        {
            "location": "Chest Freezer",
            "products": [
                {
                    "name": "Ribeye",
                    "amount": 2,
                    "stock_unit": "count",
                    "next_due_date": "2026-12-01",
                }
            ],
        }
    ]


@pytest.mark.asyncio
async def test_enrichment_reads_are_read_only(tools: dict[str, Any], seeded: SeededGrocy) -> None:
    await tools["grocy_convert_units"](product="ribeye", amount=1, from_unit="pack", to_unit="lb")
    await tools["grocy_product_card"](product="ribeye")
    await tools["grocy_consumption_history"](product="ribeye")
    await tools["grocy_stock_value"](by_location=True)
    await tools["grocy_restock_suggestions"](include_due_soon=True)
    await tools["grocy_stock_by_location"]()
    assert seeded.requests  # sanity
    assert all(r.method == "GET" for r in seeded.requests)


@pytest.mark.asyncio
async def test_product_card_unknown_product(tools: dict[str, Any], seeded: SeededGrocy) -> None:
    res = await tools["grocy_product_card"](product="nonexistent")
    assert res["error"]["code"] == "product_not_found"


# ─────────────────────────────────────────────────────────────────────────
# Unit-conversion authoring (handoff #3)
# ─────────────────────────────────────────────────────────────────────────
def _convs(seeded: SeededGrocy) -> list[dict[str, Any]]:
    return seeded.collections["quantity_unit_conversions"]


@pytest.mark.asyncio
async def test_set_conversion_updates_existing_no_duplicate(
    tools: dict[str, Any], seeded: SeededGrocy
) -> None:
    before = len(_convs(seeded))  # seeded has pack→lb (id 1) + global lb→oz (id 2)
    res = await tools["grocy_set_unit_conversion"](
        product="ribeye", from_unit="pack", to_unit="lb", factor=3.5
    )
    assert res["result"] == "updated"
    assert res["conversion_id"] == 1
    assert len(_convs(seeded)) == before  # no duplicate row
    # round-trips through the reader
    conv = await tools["grocy_convert_units"](
        product="ribeye", amount=1, from_unit="pack", to_unit="lb"
    )
    assert conv["amount_out"] == 3.5
    assert conv["conversion_source"] == "product_specific"


@pytest.mark.asyncio
async def test_set_conversion_creates_product_specific(
    tools: dict[str, Any], seeded: SeededGrocy
) -> None:
    res = await tools["grocy_set_unit_conversion"](
        product="ribeye", from_unit="count", to_unit="oz", factor=8
    )
    assert res["result"] == "created"
    assert res["product"]["name"] == "Ribeye"
    conv = await tools["grocy_convert_units"](
        product="ribeye", amount=1, from_unit="count", to_unit="oz"
    )
    assert conv["amount_out"] == 8
    assert conv["conversion_source"] == "product_specific"


@pytest.mark.asyncio
async def test_set_conversion_global(tools: dict[str, Any], seeded: SeededGrocy) -> None:
    res = await tools["grocy_set_unit_conversion"](from_unit="count", to_unit="pack", factor=12)
    assert res["result"] == "created"
    assert res["product"] is None
    conv = await tools["grocy_convert_units"](
        product="ribeye", amount=1, from_unit="count", to_unit="pack"
    )
    assert conv["amount_out"] == 12
    assert conv["conversion_source"] == "global"


@pytest.mark.asyncio
async def test_set_conversion_inverse_resolves_without_second_row(
    tools: dict[str, Any], seeded: SeededGrocy
) -> None:
    # seeded pack→lb = 4; lb→pack must resolve to 0.25 with no extra row written.
    before = len(_convs(seeded))
    conv = await tools["grocy_convert_units"](
        product="ribeye", amount=2, from_unit="lb", to_unit="pack"
    )
    assert conv["amount_out"] == 0.5
    assert len(_convs(seeded)) == before


@pytest.mark.asyncio
async def test_set_conversion_factor_invalid(tools: dict[str, Any], seeded: SeededGrocy) -> None:
    res = await tools["grocy_set_unit_conversion"](
        product="ribeye", from_unit="pack", to_unit="lb", factor=0
    )
    assert res["error"]["code"] == "factor_invalid"


@pytest.mark.asyncio
async def test_set_conversion_unit_not_found(tools: dict[str, Any], seeded: SeededGrocy) -> None:
    res = await tools["grocy_set_unit_conversion"](
        product="ribeye", from_unit="pack", to_unit="furlong", factor=2
    )
    assert res["error"]["code"] == "unit_not_found"


@pytest.mark.asyncio
async def test_list_unit_conversions_for_product(
    tools: dict[str, Any], seeded: SeededGrocy
) -> None:
    res = await tools["grocy_list_unit_conversions"](product="ribeye")
    assert len(res["conversions"]) == 1
    row = res["conversions"][0]
    assert row["from_unit"] == "pack"
    assert row["to_unit"] == "lb"
    assert row["factor"] == 4
    assert row["product"]["name"] == "Ribeye"
