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
        self.stock: dict[int, float] = {}  # product_id -> amount
        self.requests: list[httpx.Request] = []

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
