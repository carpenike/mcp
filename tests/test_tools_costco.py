"""costco_* tools — the lading store, read-only.

No database and no network: the judgement is tested in test_purchases.py, and
the tools run against a FakeReader. CI must never touch Postgres and must
never touch costco.com.

Fakes are built from the module's own column tuple where one exists, for the
reason recorded in `costco.TENDER_COLUMNS`: a fake richer than the query hides
a KeyError that every real call would hit.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, date, datetime, timedelta
from typing import Any

import pytest

from homelab_mcp.config import Settings
from homelab_mcp.tools import costco
from homelab_mcp.tools._purchases import Charge
from homelab_mcp.tools.costco import register

DSN = "postgresql://reader@localhost/lading"
TODAY = datetime.now(UTC).date()


class CapturingMCP:
    def __init__(self) -> None:
        self.tools: dict[str, Callable[..., Any]] = {}

    def tool(
        self, *, name: str, description: str = "", annotations: Any = None
    ) -> Callable[..., Any]:
        def deco(fn: Callable[..., Any]) -> Callable[..., Any]:
            self.tools[name] = fn
            return fn

        return deco


class FakeReader:
    """Stands in for the asyncpg pool, returning canned rows per query."""

    def __init__(self, **by_table: Any) -> None:
        self.by_table = by_table
        self.raises: Exception | None = None
        self.queries: list[str] = []

    async def fetch(self, query: str, *args: Any) -> list[dict[str, Any]]:
        if self.raises:
            raise self.raises
        self.queries.append(query)
        if "FROM ingest_runs" in query:
            return self.by_table.get(
                "runs",
                [
                    {
                        "account": "ryan",
                        "source": "costco",
                        "finished_at": datetime.now(UTC) - timedelta(hours=2),
                        "status": "ok",
                        "records_changed": 3,
                        "parsers_pending": 0,
                        "error": None,
                    }
                ],
            )
        if "DISTINCT period FROM sync_coverage" in query:
            return self.by_table.get("coverage", [{"period": TODAY.replace(day=1)}])
        if "FROM sync_coverage" in query:
            return self.by_table.get("coverage_summary", [])
        # Two different queries hit costco_tenders; the match one joins.
        if "FROM costco_tenders t" in query:
            return self.by_table.get("candidates", [])
        if "FROM costco_tenders" in query:
            return self.by_table.get("tenders", [])
        if "FROM costco_items i" in query or "FROM costco_items" in query:
            return self.by_table.get("items", [])
        if "FROM costco_receipts" in query:
            return self.by_table.get("receipts", [])
        return []

    async def fetchval(self, query: str, *args: Any) -> Any:
        if self.raises:
            raise self.raises
        self.queries.append(query)
        return self.by_table.get("count", 0)


def build(
    monkeypatch: pytest.MonkeyPatch,
    reader: FakeReader,
    *,
    dsn: str = DSN,
    last4: str = '{"joint checking": "4772"}',
    old_dsn_name: bool = False,
) -> dict[str, Callable[..., Any]]:
    """Register the category against a fake reader and return its tools."""
    monkeypatch.setenv("HOMELAB_MCP_OAUTH_REQUIRED", "false")
    if dsn:
        monkeypatch.setenv(
            "HOMELAB_MCP_AMAZON_DATABASE_URL"
            if old_dsn_name
            else "HOMELAB_MCP_LADING_DATABASE_URL",
            dsn,
        )
    if last4:
        monkeypatch.setenv("HOMELAB_MCP_LADING_ACCOUNT_LAST4", last4)
    monkeypatch.setattr(costco, "Reader", lambda _dsn: reader)
    mcp = CapturingMCP()
    register(mcp, Settings(_env_file=None))  # type: ignore[arg-type,call-arg]
    return mcp.tools


def tender(
    *,
    tender_id: int = 1,
    barcode: str = "20000000000000000000001",
    cents: int = 21918,
    card: str | None = "4772",
    day: date | None = None,
    tender_count: int = 1,
) -> dict[str, Any]:
    """A match candidate, shaped exactly as TENDER_COLUMNS selects it."""
    return {
        "tender_id": tender_id,
        "account": "ryan",
        "transaction_barcode": barcode,
        "amount_cents": cents,  # POSITIVE, as Costco stores it
        "card_last_4": card,
        "tender_description": "COSTCO VISA",
        "transaction_date": day or TODAY,
        "warehouse_name": "TESTVILLE",
        "total_cents": 21918,
        "total_item_count": 12,
        "tender_count": tender_count,
    }


def charge(
    ref: str, amount: float, *, day: date | None = None, account: str | None = None
) -> Charge:
    """A ledger charge, as FastMCP would have validated it."""
    return Charge(ref=ref, date=(day or TODAY).isoformat(), amount=amount, account=account)


class TestRegistration:
    def test_no_dsn_registers_nothing(self, monkeypatch: pytest.MonkeyPatch) -> None:
        assert build(monkeypatch, FakeReader(), dsn="") == {}

    def test_the_old_amazon_dsn_name_still_works(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """One database, two sources — the fallback keeps a deployment working."""
        tools = build(monkeypatch, FakeReader(), old_dsn_name=True)
        assert "costco_match_charges" in tools

    def test_registers_the_expected_surface(self, monkeypatch: pytest.MonkeyPatch) -> None:
        tools = build(monkeypatch, FakeReader())
        assert set(tools) == {
            "costco_get_sync_status",
            "costco_match_charges",
            "costco_get_receipt",
            "costco_search_items",
            "costco_list_receipts",
            "costco_price_history",
        }


class TestMatchCharges:
    async def test_verified_card_is_exact(self, monkeypatch: pytest.MonkeyPatch) -> None:
        tools = build(monkeypatch, FakeReader(candidates=[tender()]))
        out = await tools["costco_match_charges"](
            charges=[charge("t1", -219.18, account="joint checking")]
        )
        entry = out["results"][0]
        assert entry["confidence"] == "exact"
        assert entry["candidates"][0]["card_last_4"] == "4772"
        assert entry["candidates"][0]["amount"] == 219.18

    async def test_a_negative_charge_matches_a_positive_stored_amount(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Costco stores every amount positive; Actual signs a spend negative.

        The sign convention differs from the amazon_* store deliberately (see
        lading migration 0004), so this is the test that catches a regression
        where the query stops taking the absolute value and silently matches
        nothing at all.
        """
        reader = FakeReader(candidates=[tender(cents=21918)])
        tools = build(monkeypatch, reader)
        out = await tools["costco_match_charges"](charges=[charge("t1", -219.18)])
        assert out["results"][0]["confidence"] == "probable"

    async def test_a_refund_is_refused_by_name(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """A positive ledger amount must not be paired with a same-value purchase."""
        reader = FakeReader(candidates=[tender()])
        tools = build(monkeypatch, reader)
        out = await tools["costco_match_charges"](charges=[charge("r1", 219.18)])
        entry = out["results"][0]
        assert entry["confidence"] == "none"
        assert entry["reason"] == "refund_unsupported"
        assert entry["candidates"] == []
        # And it never even asked the database about it.
        assert not any("costco_tenders t" in q for q in reader.queries)

    async def test_uncovered_month_is_not_reported_as_no_match(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        tools = build(
            monkeypatch, FakeReader(candidates=[], coverage=[{"period": date(2026, 8, 1)}])
        )
        out = await tools["costco_match_charges"](
            charges=[charge("old", -50.0, day=date(2024, 3, 4))]
        )
        assert out["results"][0]["reason"] == "outside_coverage"

    async def test_split_tender_surfaces_that_the_total_is_not_the_charge(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """tender_count > 1 is how the caller learns the rest is elsewhere."""
        tools = build(
            monkeypatch,
            FakeReader(candidates=[tender(cents=11000, tender_count=2)]),
        )
        out = await tools["costco_match_charges"](charges=[charge("t1", -110.00)])
        cand = out["results"][0]["candidates"][0]
        assert cand["tender_count"] == 2
        assert cand["amount"] == 110.00
        assert cand["receipt_total"] == 219.18

    async def test_two_charges_one_tender_is_oversubscribed(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        tools = build(monkeypatch, FakeReader(candidates=[tender(tender_id=7)]))
        out = await tools["costco_match_charges"](
            charges=[
                charge("a", -219.18),
                charge("b", -219.18),
            ]
        )
        assert out["oversubscribed"] == 2
        assert all(r["confidence"] == "ambiguous" for r in out["results"])

    async def test_batch_cap_is_enforced(self, monkeypatch: pytest.MonkeyPatch) -> None:
        tools = build(monkeypatch, FakeReader())
        out = await tools["costco_match_charges"](charges=[charge(str(i), -1.0) for i in range(51)])
        assert out["error"]["code"] == "too_many_charges"

    async def test_window_is_a_day_not_three(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """A warehouse purchase settles at the register — no capture lag."""
        tools = build(monkeypatch, FakeReader(candidates=[tender()]))
        out = await tools["costco_match_charges"](charges=[charge("t1", -219.18)])
        assert out["match_window_days"] == 1

    async def test_store_failure_maps_to_the_error_contract(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        reader = FakeReader()
        reader.raises = ConnectionError("down")
        tools = build(monkeypatch, reader)
        out = await tools["costco_match_charges"](charges=[charge("t1", -219.18)])
        assert out["error"]["code"] == "lading_unreachable"


class TestGetReceipt:
    async def test_unknown_barcode_says_not_stored(self, monkeypatch: pytest.MonkeyPatch) -> None:
        tools = build(monkeypatch, FakeReader(receipts=[]))
        out = await tools["costco_get_receipt"](transaction_barcode="20000000000000000000009")
        assert out["found"] is False
        assert out["reason"] == "not_stored"

    async def test_a_malformed_barcode_is_rejected_at_the_boundary(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        tools = build(monkeypatch, FakeReader())
        out = await tools["costco_get_receipt"](transaction_barcode="'; DROP TABLE --")
        assert out["error"]["code"] == "bad_barcode"

    async def test_returns_items_and_tenders(self, monkeypatch: pytest.MonkeyPatch) -> None:
        reader = FakeReader(
            receipts=[
                {
                    "account": "ryan",
                    "transaction_barcode": "20000000000000000000001",
                    "transaction_datetime": datetime(2026, 6, 30, 22, 30),
                    "transaction_date": date(2026, 6, 30),
                    "warehouse_name": "TESTVILLE",
                    "warehouse_number": 330,
                    "total_cents": 21918,
                    "subtotal_cents": 19999,
                    "taxes_cents": 1919,
                    "instant_savings_cents": 0,
                    "total_item_count": 2,
                    "tender_count": 1,
                    "transaction_type": "Sales",
                }
            ],
            items=[
                {
                    "account": "ryan",
                    "transaction_barcode": "20000000000000000000001",
                    "position": 0,
                    "description": "TEST ITEM",
                    "item_number": "1234567",
                    "quantity": 2,
                    "amount_cents": 1178,
                    "unit_price_cents": 589,
                }
            ],
            tenders=[
                {
                    "account": "ryan",
                    "transaction_barcode": "20000000000000000000001",
                    "position": 0,
                    "tender_description": "COSTCO VISA",
                    "card_last_4": "4772",
                    "amount_cents": 21918,
                }
            ],
        )
        tools = build(monkeypatch, reader)
        out = await tools["costco_get_receipt"](transaction_barcode="20000000000000000000001")
        receipt = out["receipts"][0]
        assert out["found"] is True
        assert receipt["items"][0]["quantity"] == 2
        assert receipt["tenders"][0]["card_last_4"] == "4772"
        assert receipt["taxes"] == 19.19

    async def test_evening_time_is_rendered_as_stored_not_shifted(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The timestamp is warehouse-local and tz-naive by design."""
        reader = FakeReader(
            receipts=[
                {
                    "account": "ryan",
                    "transaction_barcode": "20000000000000000000001",
                    "transaction_datetime": datetime(2026, 6, 30, 22, 30),
                    "transaction_date": date(2026, 6, 30),
                    "warehouse_name": "TESTVILLE",
                    "warehouse_number": 330,
                    "total_cents": 1,
                    "subtotal_cents": 1,
                    "taxes_cents": 0,
                    "instant_savings_cents": 0,
                    "total_item_count": 0,
                    "tender_count": 1,
                    "transaction_type": "Sales",
                }
            ]
        )
        tools = build(monkeypatch, reader)
        out = await tools["costco_get_receipt"](transaction_barcode="20000000000000000000001")
        assert out["receipts"][0]["time"] == "22:30"
        assert out["receipts"][0]["date"] == "2026-06-30"


class TestListsAndSearch:
    async def test_list_truncation_is_explicit(self, monkeypatch: pytest.MonkeyPatch) -> None:
        reader = FakeReader(
            receipts=[
                {
                    "account": "ryan",
                    "transaction_barcode": "20000000000000000000001",
                    "transaction_date": TODAY,
                    "warehouse_name": "TESTVILLE",
                    "total_cents": 21918,
                    "total_item_count": 12,
                    "tender_count": 1,
                }
            ],
            count=40,
        )
        tools = build(monkeypatch, reader)
        out = await tools["costco_list_receipts"](limit=1)
        assert out["returned"] == 1
        assert out["total"] == 40
        assert out["truncated"] is True

    async def test_search_returns_items_with_their_trip(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        reader = FakeReader(
            items=[
                {
                    "account": "ryan",
                    "transaction_barcode": "20000000000000000000001",
                    "description": "OLIVE OIL",
                    "item_number": "1234567",
                    "quantity": 1,
                    "amount_cents": 1899,
                    "unit_price_cents": 1899,
                    "transaction_date": TODAY,
                    "warehouse_name": "TESTVILLE",
                }
            ],
            count=1,
        )
        tools = build(monkeypatch, reader)
        out = await tools["costco_search_items"](query="olive oil")
        assert out["items"][0]["description"] == "OLIVE OIL"
        assert out["items"][0]["amount"] == 18.99
        assert out["truncated"] is False

    async def test_a_bad_since_date_is_rejected(self, monkeypatch: pytest.MonkeyPatch) -> None:
        tools = build(monkeypatch, FakeReader())
        out = await tools["costco_list_receipts"](since="08/01/2026")
        assert out["error"]["code"] == "bad_date"


class TestSyncStatus:
    async def test_reports_freshness_and_coverage(self, monkeypatch: pytest.MonkeyPatch) -> None:
        reader = FakeReader(
            coverage_summary=[
                {
                    "account": "ryan",
                    "covered_from": date(2025, 8, 1),
                    "covered_to": date(2026, 8, 1),
                    "months": 13,
                }
            ]
        )
        tools = build(monkeypatch, reader)
        out = await tools["costco_get_sync_status"]()
        assert out["stale"] is False
        assert out["coverage"][0]["months"] == 13
        assert out["stale_after_hours"] == 36

    async def test_goes_stale_past_the_window(self, monkeypatch: pytest.MonkeyPatch) -> None:
        reader = FakeReader(
            runs=[
                {
                    "account": "ryan",
                    "source": "costco",
                    "finished_at": datetime.now(UTC) - timedelta(hours=80),
                    "status": "ok",
                    "records_changed": 0,
                    "parsers_pending": 0,
                    "error": None,
                }
            ]
        )
        tools = build(monkeypatch, reader)
        out = await tools["costco_get_sync_status"]()
        assert out["stale"] is True

    async def test_a_never_run_sync_is_stale_not_silently_fine(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        tools = build(monkeypatch, FakeReader(runs=[]))
        out = await tools["costco_get_sync_status"]()
        assert out["stale"] is True
        assert out["data_as_of"] is None


def price_row(
    *,
    description: str = "PORK BELLY",
    item_number: str = "10299",
    day: date | None = None,
    unit_cents: int = 349,
    amount_cents: int = 3049,
    account: str = "ryan",
) -> dict[str, Any]:
    """One priced line, shaped as the price_history query selects it."""
    return {
        "description": description,
        "item_number": item_number,
        "quantity": 1,
        "amount_cents": amount_cents,
        "unit_price_cents": unit_cents,
        "fuel_quantity": None,
        "transaction_date": day or TODAY,
        "warehouse_name": "TESTVILLE",
        "account": account,
    }


class TestPriceHistory:
    """The question this data actually gets asked.

    Every case here is a way the naive approach (search_items plus arithmetic
    in the model) gets a confident wrong answer.
    """

    async def test_two_products_do_not_become_one_trend(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A broad query matches several products; averaging them is nonsense."""
        reader = FakeReader(
            items=[
                price_row(description="CHICKEN THIGH", unit_cents=299, day=date(2024, 1, 1)),
                price_row(description="CHICKEN THIGH", unit_cents=399, day=date(2026, 1, 1)),
                price_row(description="CHICKEN STOCK", unit_cents=999, day=date(2024, 1, 1)),
            ]
        )
        tools = build(monkeypatch, reader)
        out = await tools["costco_price_history"](query="chicken")
        assert out["distinct_products"] == 2
        by = {p["description"]: p for p in out["products"]}
        assert by["CHICKEN THIGH"]["observations"] == 2
        assert by["CHICKEN STOCK"]["observations"] == 1

    async def test_change_is_computed_from_unit_price(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        reader = FakeReader(
            items=[
                price_row(unit_cents=349, day=date(2023, 10, 23)),
                price_row(unit_cents=449, day=date(2025, 9, 19)),
            ]
        )
        tools = build(monkeypatch, reader)
        out = await tools["costco_price_history"](query="pork belly")
        p = out["products"][0]
        assert p["first"]["unit_price"] == 3.49
        assert p["latest"]["unit_price"] == 4.49
        assert p["change"] == {"absolute": 1.0, "percent": 28.7}

    async def test_series_is_oldest_first(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """A trend reads left to right; newest-first would invert the sign."""
        reader = FakeReader(
            items=[
                price_row(unit_cents=349, day=date(2023, 10, 23)),
                price_row(unit_cents=449, day=date(2025, 9, 19)),
            ]
        )
        tools = build(monkeypatch, reader)
        out = await tools["costco_price_history"](query="pork belly")
        dates = [x["date"] for x in out["products"][0]["series"]]
        assert dates == sorted(dates)

    async def test_a_renumbered_item_is_flagged_not_split(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Costco renumbers products; the series must stay continuous."""
        reader = FakeReader(
            items=[
                price_row(item_number="10299", unit_cents=349, day=date(2024, 2, 9)),
                price_row(item_number="18316", unit_cents=449, day=date(2025, 2, 2)),
            ]
        )
        tools = build(monkeypatch, reader)
        p = (await tools["costco_price_history"](query="pork belly"))["products"][0]
        assert p["renumbered"] is True
        assert sorted(p["item_numbers"]) == ["10299", "18316"]
        assert p["observations"] == 2

    async def test_weight_is_recovered_for_weighed_goods(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """`quantity` is 1 on weighed items; the pounds are amount/unit_price."""
        reader = FakeReader(items=[price_row(unit_cents=449, amount_cents=2321)])
        tools = build(monkeypatch, reader)
        p = (await tools["costco_price_history"](query="pork belly"))["products"][0]
        assert p["series"][0]["units"] == 5.169
        assert p["series"][0]["unit_price"] == 4.49

    async def test_both_accounts_feed_one_series(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Two memberships, one household, one price history."""
        reader = FakeReader(
            items=[
                price_row(day=date(2024, 1, 1), account="ryan"),
                price_row(day=date(2025, 1, 1), account="steffi"),
            ]
        )
        tools = build(monkeypatch, reader)
        p = (await tools["costco_price_history"](query="pork belly"))["products"][0]
        assert {x["account"] for x in p["series"]} == {"ryan", "steffi"}

    async def test_a_bad_date_is_rejected(self, monkeypatch: pytest.MonkeyPatch) -> None:
        tools = build(monkeypatch, FakeReader())
        out = await tools["costco_price_history"](query="pork belly", since="10/23/2023")
        assert out["error"]["code"] == "bad_date"

    async def test_no_matches_is_empty_not_an_error(self, monkeypatch: pytest.MonkeyPatch) -> None:
        tools = build(monkeypatch, FakeReader(items=[]))
        out = await tools["costco_price_history"](query="unobtainium")
        assert out["products"] == []
        assert out["distinct_products"] == 0
        assert out["truncated"] is False


class TestSearchDateFilters:
    async def test_search_accepts_a_date_range(self, monkeypatch: pytest.MonkeyPatch) -> None:
        reader = FakeReader(items=[], count=0)
        tools = build(monkeypatch, reader)
        out = await tools["costco_search_items"](
            query="olive oil", since="2024-01-01", until="2025-01-01"
        )
        assert out["returned"] == 0
        assert any("transaction_date >=" in q for q in reader.queries)

    async def test_search_rejects_a_bad_date(self, monkeypatch: pytest.MonkeyPatch) -> None:
        tools = build(monkeypatch, FakeReader())
        out = await tools["costco_search_items"](query="olive oil", since="not-a-date")
        assert out["error"]["code"] == "bad_date"
