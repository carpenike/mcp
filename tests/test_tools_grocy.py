"""Grocy tool tests.

Covers:
  * Input-validation guards that short-circuit BEFORE any HTTP call
    (exactly-one-of product_id/barcode, missing API key) — these never
    touch the wire.
  * Path/auth construction and body shaping, driven by an in-memory
    FakeGrocy callback that mirrors the real wire (GROCY-API-KEY header,
    array stock-log responses, 204 No Content, 400 error_message).
  * The barcode path-encoding guard (no traversal reaches the URL).
"""

from __future__ import annotations

import json
from typing import Any
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest

from homelab_mcp.config import Settings
from homelab_mcp.tools.grocy import _compact, _enc, register

API_KEY = "test-key-123"
BASE = "https://grocy.test"

# Several tool paths return validation errors BEFORE any HTTP call, so the
# catch-all callback legitimately goes unused in those tests.
pytestmark = pytest.mark.httpx_mock(assert_all_responses_were_requested=False)


# ─────────────────────────────────────────────────────────────────────────
# Pure helpers
# ─────────────────────────────────────────────────────────────────────────
def test_compact_drops_none() -> None:
    assert _compact({"a": 1, "b": None, "c": 0, "d": False}) == {"a": 1, "c": 0, "d": False}


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("012345", "012345"),
        ("../etc/passwd", "..%2Fetc%2Fpasswd"),
        ("a/b", "a%2Fb"),
        ("a b", "a%20b"),
        ("a?b#c", "a%3Fb%23c"),
    ],
)
def test_enc_encodes_path_segment(raw: str, expected: str) -> None:
    """_enc leaves no separators that could break out of the path segment."""
    assert _enc(raw) == expected


# ─────────────────────────────────────────────────────────────────────────
# Fake Grocy server + fixtures
# ─────────────────────────────────────────────────────────────────────────
class FakeGrocy:
    """In-memory Grocy that records requests and returns canned responses."""

    def __init__(self) -> None:
        self.requests: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        path = urlsplit(str(request.url)).path

        # Auth is mandatory on every route.
        if request.headers.get("GROCY-API-KEY") != API_KEY:
            return httpx.Response(400, json={"error_message": "Invalid API key"})

        if path == "/stock" and request.method == "GET":
            return httpx.Response(200, json=[{"product_id": 1, "amount": 2}])
        if path == "/stock/volatile" and request.method == "GET":
            return httpx.Response(
                200,
                json={
                    "due_products": [],
                    "overdue_products": [],
                    "expired_products": [],
                    "missing_products": [],
                },
            )
        if path.endswith("/add") or path.endswith("/consume") or path.endswith("/open"):
            return httpx.Response(200, json=[{"id": 99}])
        if path.startswith("/stock/shoppinglist/"):
            return httpx.Response(204)
        if path == "/objects/shopping_list" and request.method == "GET":
            return httpx.Response(200, json=[{"id": 1, "product_id": 5, "amount": 3}])
        if path == "/chores" and request.method == "GET":
            return httpx.Response(200, json=[{"chore_id": 1, "chore_name": "Vacuum"}])
        if path.startswith("/chores/") and path.endswith("/execute"):
            return httpx.Response(200, json={"id": 7})
        if path.startswith("/chores/"):
            return httpx.Response(200, json={"chore": {"id": 1}})
        if path == "/tasks" and request.method == "GET":
            return httpx.Response(200, json=[{"id": 1, "name": "Pay rent"}])
        if path.startswith("/tasks/") and path.endswith("/complete"):
            return httpx.Response(204)

        return httpx.Response(404, json={"error_message": f"unhandled {request.method} {path}"})


class CapturingMCP:
    """Captures the handlers registered via @mcp.tool(name=...)."""

    def __init__(self) -> None:
        self.tools: dict[str, Any] = {}

    def tool(self, *, name: str, description: str) -> Any:
        def deco(fn: Any) -> Any:
            self.tools[name] = fn
            return fn

        return deco


def _settings(api_key: str = API_KEY) -> Settings:
    return Settings(
        oauth_required=False,
        grocy_base_url=BASE,
        grocy_api_key=api_key,
    )


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


def _body(request: httpx.Request) -> Any:
    return json.loads(request.content) if request.content else None


# ─────────────────────────────────────────────────────────────────────────
# Reads
# ─────────────────────────────────────────────────────────────────────────
@pytest.mark.asyncio
async def test_list_stock(tools: dict[str, Any], fake: FakeGrocy) -> None:
    res = await tools["grocy_list_stock"]()
    assert res == {"count": 1, "products": [{"product_id": 1, "amount": 2}]}
    assert fake.requests[0].headers["GROCY-API-KEY"] == API_KEY


@pytest.mark.asyncio
async def test_list_volatile_passes_due_soon_days(tools: dict[str, Any], fake: FakeGrocy) -> None:
    res = await tools["grocy_list_volatile_stock"](due_soon_days=10)
    assert set(res) == {"due_products", "overdue_products", "expired_products", "missing_products"}
    qs = parse_qs(urlsplit(str(fake.requests[0].url)).query)
    assert qs["due_soon_days"] == ["10"]


@pytest.mark.asyncio
async def test_get_product_by_id(tools: dict[str, Any], fake: FakeGrocy) -> None:
    await tools["grocy_get_product"](product_id=1)
    assert urlsplit(str(fake.requests[0].url)).path == "/stock/products/1"


@pytest.mark.asyncio
async def test_get_product_by_barcode_is_encoded(tools: dict[str, Any], fake: FakeGrocy) -> None:
    await tools["grocy_get_product"](barcode="../../etc/passwd")
    path = urlsplit(str(fake.requests[0].url)).path
    # The traversal sequence must be percent-encoded, not a real path break.
    assert path == "/stock/products/by-barcode/..%2F..%2Fetc%2Fpasswd"


@pytest.mark.asyncio
async def test_list_shopping_list_filters_by_list(tools: dict[str, Any], fake: FakeGrocy) -> None:
    res = await tools["grocy_list_shopping_list"](list_id=2)
    assert res["count"] == 1
    qs = parse_qs(urlsplit(str(fake.requests[0].url)).query)
    assert qs["query[]"] == ["shopping_list_id=2"]


@pytest.mark.asyncio
async def test_list_chores_and_tasks(tools: dict[str, Any], fake: FakeGrocy) -> None:
    chores = await tools["grocy_list_chores"]()
    tasks = await tools["grocy_list_tasks"]()
    assert chores["count"] == 1
    assert tasks["count"] == 1


# ─────────────────────────────────────────────────────────────────────────
# Writes
# ─────────────────────────────────────────────────────────────────────────
@pytest.mark.asyncio
async def test_add_product_builds_purchase_body(tools: dict[str, Any], fake: FakeGrocy) -> None:
    res = await tools["grocy_add_product"](product_id=3, amount=2, price=1.99)
    assert res["ok"] is True
    req = fake.requests[0]
    assert urlsplit(str(req.url)).path == "/stock/products/3/add"
    body = _body(req)
    assert body["transaction_type"] == "purchase"
    assert body["amount"] == 2
    assert body["price"] == 1.99
    assert "location_id" not in body  # None values dropped


@pytest.mark.asyncio
async def test_consume_product_by_barcode(tools: dict[str, Any], fake: FakeGrocy) -> None:
    res = await tools["grocy_consume_product"](barcode="012345", amount=1, spoiled=True)
    assert res["ok"] is True
    req = fake.requests[0]
    assert urlsplit(str(req.url)).path == "/stock/products/by-barcode/012345/consume"
    body = _body(req)
    assert body["transaction_type"] == "consume"
    assert body["spoiled"] is True


@pytest.mark.asyncio
async def test_track_chore_returns_log(tools: dict[str, Any], fake: FakeGrocy) -> None:
    res = await tools["grocy_track_chore"](chore_id=1, skipped=True)
    assert res == {"ok": True, "log_entry": {"id": 7}}
    assert _body(fake.requests[0])["skipped"] is True


@pytest.mark.asyncio
async def test_complete_task_204(tools: dict[str, Any], fake: FakeGrocy) -> None:
    res = await tools["grocy_complete_task"](task_id=1)
    assert res == {"ok": True}
    assert urlsplit(str(fake.requests[0].url)).path == "/tasks/1/complete"


@pytest.mark.asyncio
async def test_add_shopping_list_product(tools: dict[str, Any], fake: FakeGrocy) -> None:
    res = await tools["grocy_add_shopping_list_product"](product_id=5, amount=4, note="org")
    assert res == {"ok": True}
    body = _body(fake.requests[0])
    assert body == {"product_id": 5, "product_amount": 4, "note": "org"}


# ─────────────────────────────────────────────────────────────────────────
# Guard rails
# ─────────────────────────────────────────────────────────────────────────
@pytest.mark.asyncio
async def test_get_product_requires_exactly_one_identifier(tools: dict[str, Any]) -> None:
    both = await tools["grocy_get_product"](product_id=1, barcode="x")
    neither = await tools["grocy_get_product"]()
    assert "error" in both
    assert "error" in neither


@pytest.mark.asyncio
async def test_missing_api_key_short_circuits(httpx_mock: Any) -> None:
    mcp = CapturingMCP()
    register(mcp, _settings(api_key=""))  # type: ignore[arg-type]
    res = await mcp.tools["grocy_list_stock"]()
    assert "error" in res
    assert "API key" in res["error"]
    # No HTTP request should have been attempted.
    assert not httpx_mock.get_requests()


@pytest.mark.asyncio
async def test_upstream_400_is_surfaced_as_error(tools: dict[str, Any], httpx_mock: Any) -> None:
    httpx_mock.add_response(
        url=f"{BASE}/chores/9",
        status_code=400,
        json={"error_message": "Not existing chore"},
    )
    res = await tools["grocy_get_chore"](chore_id=9)
    assert "error" in res
    assert "Not existing chore" in res["error"]
