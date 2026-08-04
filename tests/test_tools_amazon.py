"""amazon_* tools — the lading store, read-only.

No database and no network: the matcher is a pure function and the tools run
against a FakeReader. CI must never touch Postgres, and it must obviously
never touch amazon.com.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, date, datetime, timedelta
from typing import Any

import pytest

from homelab_mcp.config import Settings
from homelab_mcp.tools import amazon
from homelab_mcp.tools.amazon import (
    Charge,
    flag_oversubscribed,
    funding_of,
    match_charge,
    none_reason,
    register,
    to_cents,
)

DSN = "postgresql://reader@localhost/lading"


class CapturingMCP:
    """Collects tools registered via @mcp.tool(name=...) so tests can call them."""

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
                        "source": "transactions",
                        "finished_at": datetime.now(UTC) - timedelta(hours=2),
                        "status": "ok",
                        "records_changed": 3,
                        "parsers_pending": 0,
                        "error": None,
                    }
                ],
            )
        if "DISTINCT period FROM sync_coverage" in query:
            return self.by_table.get("coverage", [{"period": date(2026, 8, 1)}])
        if "FROM sync_coverage GROUP BY" in query:
            return self.by_table.get("coverage_summary", [])
        if "FROM amazon_transactions" in query:
            return self.by_table.get("transactions", [])
        if "FROM amazon_orders" in query:
            return self.by_table.get("orders", [])
        if "FROM amazon_items" in query:
            return self.by_table.get("items", [])
        if "FROM amazon_shipments" in query:
            return self.by_table.get("shipments", [])
        return []

    async def fetchrow(self, query: str, *args: Any) -> dict[str, Any] | None:
        rows = await self.fetch(query, *args)
        return rows[0] if rows else None

    async def fetchval(self, query: str, *args: Any) -> Any:
        if self.raises:
            raise self.raises
        self.queries.append(query)
        return self.by_table.get("scalar", 0)


def build(
    monkeypatch: pytest.MonkeyPatch,
    reader: FakeReader,
    *,
    dsn: str = DSN,
    last4: str = "",
) -> dict[str, Callable[..., Any]]:
    """Register the category against a fake reader and return its tools."""
    monkeypatch.setenv("HOMELAB_MCP_OAUTH_REQUIRED", "false")
    monkeypatch.setenv("HOMELAB_MCP_AMAZON_DATABASE_URL", dsn)
    if last4:
        monkeypatch.setenv("HOMELAB_MCP_AMAZON_ACCOUNT_LAST4", last4)
    monkeypatch.setattr(amazon, "Reader", lambda _dsn: reader)
    mcp = CapturingMCP()
    register(mcp, Settings())  # type: ignore[arg-type]
    return mcp.tools


def txn(**over: Any) -> dict[str, Any]:
    base = {
        "id": 1,
        "account": "ryan",
        "completed_date": date(2026, 8, 1),
        "amount_cents": -8431,
        "is_refund": False,
        "payment_method": "Prime Visa ****4772",
        "payment_method_last_4": "4772",
        "seller": "Amazon.com",
        "order_number": "111-2223334-4445556",
    }
    base.update(over)
    return base


# ── registration ─────────────────────────────────────────────────────


def test_category_does_not_register_without_a_dsn(monkeypatch: pytest.MonkeyPatch) -> None:
    """An unconfigured upstream means no tools, not a broken server."""
    monkeypatch.setenv("HOMELAB_MCP_OAUTH_REQUIRED", "false")
    monkeypatch.setenv("HOMELAB_MCP_AMAZON_DATABASE_URL", "")
    mcp = CapturingMCP()
    register(mcp, Settings())  # type: ignore[arg-type]
    assert mcp.tools == {}


def test_registers_all_five_tools(monkeypatch: pytest.MonkeyPatch) -> None:
    assert sorted(build(monkeypatch, FakeReader())) == [
        "amazon_get_order",
        "amazon_get_sync_status",
        "amazon_list_orders",
        "amazon_match_charges",
        "amazon_search_items",
    ]


# ── the pure matcher ─────────────────────────────────────────────────


class TestMatchCharge:
    def test_one_candidate_with_matching_card_is_exact(self) -> None:
        conf, chosen = match_charge(-8431, date(2026, 8, 1), [txn()], expected_last_4="4772")
        assert conf == "exact"
        assert len(chosen) == 1

    def test_unknown_card_is_probable_not_exact(self) -> None:
        # The account was not named, or has no configured last-4. One
        # plausible answer, but the card was never verified.
        conf, _ = match_charge(-8431, date(2026, 8, 1), [txn()], expected_last_4=None)
        assert conf == "probable"

    def test_candidate_without_a_card_cannot_be_verified(self) -> None:
        # A balance-funded row has no last-4, so the filter is unusable and
        # must not silently drop it.
        cands = [txn(payment_method="Amazon Gift Card", payment_method_last_4=None)]
        conf, chosen = match_charge(-8431, date(2026, 8, 1), cands, expected_last_4="4772")
        assert conf == "probable"
        assert len(chosen) == 1

    def test_two_candidates_same_order_is_probable(self) -> None:
        cands = [txn(id=1), txn(id=2)]
        conf, chosen = match_charge(-8431, date(2026, 8, 1), cands, expected_last_4=None)
        assert conf == "probable"
        assert len(chosen) == 2

    def test_two_candidates_different_orders_is_ambiguous(self) -> None:
        cands = [txn(id=1, order_number="111-A"), txn(id=2, order_number="111-B")]
        conf, chosen = match_charge(-8431, date(2026, 8, 1), cands, expected_last_4=None)
        assert conf == "ambiguous"
        # Both returned; the tool must never pick.
        assert len(chosen) == 2

    def test_card_disambiguates_two_orders(self) -> None:
        cands = [
            txn(id=1, order_number="111-A", payment_method_last_4="4772"),
            txn(id=2, order_number="111-B", payment_method_last_4="1111"),
        ]
        conf, chosen = match_charge(-8431, date(2026, 8, 1), cands, expected_last_4="4772")
        assert conf == "exact"
        assert [c["order_number"] for c in chosen] == ["111-A"]

    def test_no_candidate_matches_the_known_card(self) -> None:
        # Evidence against all of them, not for one.
        cands = [txn(id=1, order_number="111-A", payment_method_last_4="1111")]
        conf, chosen = match_charge(-8431, date(2026, 8, 1), cands, expected_last_4="4772")
        assert conf == "ambiguous"
        assert len(chosen) == 1

    def test_no_candidates_is_none(self) -> None:
        assert match_charge(-8431, date(2026, 8, 1), [], expected_last_4="4772") == ("none", [])

    def test_two_accounts_same_amount_is_ambiguous(self) -> None:
        # A joint card and two Amazon accounts. Both are real possibilities.
        cands = [
            txn(id=1, account="ryan", order_number="111-A"),
            txn(id=2, account="steffi", order_number="111-B"),
        ]
        conf, chosen = match_charge(-8431, date(2026, 8, 1), cands, expected_last_4=None)
        assert conf == "ambiguous"
        assert {c["account"] for c in chosen} == {"ryan", "steffi"}


class TestNoneReason:
    COVERED = {date(2026, 8, 1)}

    def test_uncovered_month_is_never_reported_as_no_order(self) -> None:
        # The distinction the household's money rests on.
        r = none_reason(date(2026, 3, 15), self.COVERED, today=date(2026, 8, 4), stale=False)
        assert r == "outside_coverage"

    def test_covered_month_with_nothing_matching(self) -> None:
        r = none_reason(date(2026, 8, 2), self.COVERED, today=date(2026, 8, 4), stale=False)
        assert r == "no_amount_match"

    def test_recent_charge_on_a_stale_sync(self) -> None:
        r = none_reason(date(2026, 8, 3), self.COVERED, today=date(2026, 8, 4), stale=True)
        assert r == "stale_sync"

    def test_old_charge_on_a_stale_sync_is_still_a_real_miss(self) -> None:
        # Staleness cannot explain a charge from three weeks ago.
        r = none_reason(date(2026, 8, 1), self.COVERED, today=date(2026, 8, 25), stale=True)
        assert r == "no_amount_match"


class TestFundingOf:
    def test_card(self) -> None:
        assert funding_of("Prime Visa ****4772", "4772") == "card"

    def test_balance(self) -> None:
        assert funding_of("Amazon Gift Card", None) == "balance"

    def test_unknown_rather_than_guessing(self) -> None:
        assert funding_of(None, None) == "unknown"


def test_to_cents_is_exact() -> None:
    assert to_cents(84.31) == 8431
    assert to_cents(-84.31) == -8431
    assert to_cents(0.1) == 10
    assert to_cents(1234.56) == 123456


# ── the batch tool ───────────────────────────────────────────────────


async def test_match_charges_returns_items_for_a_hit(monkeypatch: pytest.MonkeyPatch) -> None:
    reader = FakeReader(
        transactions=[txn()],
        orders=[
            {
                "account": "ryan",
                "order_number": "111-2223334-4445556",
                "order_placed_date": date(2026, 7, 30),
                "grand_total_cents": 8431,
                "gift_card_cents": None,
                "estimated_tax_cents": 633,
                "shipping_total_cents": 0,
                "promotion_cents": None,
                "coupon_savings_cents": None,
                "subscription_discount_cents": None,
                "refund_total_cents": None,
                "payment_method": "Prime Visa ****4772",
                "payment_method_last_4": "4772",
                "recipient": "A Person",
                "cancelled": False,
                "is_whole_foods": False,
                "item_count": 1,
                "full_details": True,
            }
        ],
        items=[
            {
                "account": "ryan",
                "order_number": "111-2223334-4445556",
                "position": 0,
                "title": "Furnace Filter 20x25x1",
                "asin": "B00ABCDEFG",
                "price_cents": 4299,
                "quantity": 2,
                "seller": "FilterCo",
            }
        ],
    )
    tools = build(monkeypatch, reader, last4='{"Visa": "4772"}')
    out = await tools["amazon_match_charges"](
        charges=[Charge(ref="t1", date="2026-08-01", amount=-84.31, account="Visa")]
    )
    m = out["matches"][0]
    assert m["ref"] == "t1"
    assert m["confidence"] == "exact"
    cand = m["candidates"][0]
    assert cand["funding"] == "card"
    assert cand["order"]["items"][0]["title"] == "Furnace Filter 20x25x1"
    assert cand["order"]["estimated_tax"] == 6.33
    assert out["matched"] == 1
    assert "data_as_of" in out and "stale" in out


async def test_match_charges_uncovered_month_is_flagged_not_denied(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    reader = FakeReader(transactions=[], coverage=[{"period": date(2026, 8, 1)}])
    tools = build(monkeypatch, reader)
    out = await tools["amazon_match_charges"](
        charges=[Charge(ref="t1", date="2026-02-14", amount=-20.00)]
    )
    m = out["matches"][0]
    assert m["confidence"] == "none"
    assert m["reason"] == "outside_coverage"
    assert m["candidates"] == []


async def test_match_charges_gift_card_order_reports_the_gift_card_line(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # A fully balance-funded order reports grand_total $0.00. Without the
    # gift-card figure beside it an $18 order reads as free.
    reader = FakeReader(
        transactions=[txn(payment_method="Amazon Gift Card", payment_method_last_4=None)],
        orders=[
            {
                "account": "ryan",
                "order_number": "111-2223334-4445556",
                "order_placed_date": date(2026, 7, 30),
                "grand_total_cents": 0,
                "gift_card_cents": -1803,
                "estimated_tax_cents": None,
                "shipping_total_cents": None,
                "promotion_cents": None,
                "coupon_savings_cents": None,
                "subscription_discount_cents": None,
                "refund_total_cents": None,
                "payment_method": "Amazon Gift Card",
                "payment_method_last_4": None,
                "recipient": None,
                "cancelled": False,
                "is_whole_foods": False,
                "item_count": 1,
                "full_details": True,
            }
        ],
    )
    tools = build(monkeypatch, reader)
    out = await tools["amazon_match_charges"](
        charges=[Charge(ref="t1", date="2026-08-01", amount=-84.31)]
    )
    cand = out["matches"][0]["candidates"][0]
    assert cand["funding"] == "balance"
    assert cand["order"]["grand_total"] == 0.0
    assert cand["order"]["gift_card"] == -18.03


async def test_match_charges_transaction_with_no_order(monkeypatch: pytest.MonkeyPatch) -> None:
    # A gift-card reload or a digital charge: matched, but nothing shipped.
    reader = FakeReader(transactions=[txn(order_number=None)], orders=[])
    tools = build(monkeypatch, reader)
    out = await tools["amazon_match_charges"](
        charges=[Charge(ref="t1", date="2026-08-01", amount=-84.31)]
    )
    cand = out["matches"][0]["candidates"][0]
    assert cand["order_number"] is None
    assert cand["order"] is None


async def test_match_charges_rejects_a_category_field(monkeypatch: pytest.MonkeyPatch) -> None:
    # extra="forbid": the advisor's remit is to categorize, and this tool is
    # not where that happens.
    with pytest.raises(Exception):  # noqa: B017 — pydantic ValidationError
        Charge(ref="t1", date="2026-08-01", amount=-1.0, category="Groceries")  # type: ignore[call-arg]


async def test_match_charges_store_failure_returns_the_error_contract(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    reader = FakeReader()
    reader.raises = ConnectionError("could not connect to postgresql://u:pw@h/db")
    tools = build(monkeypatch, reader)
    out = await tools["amazon_match_charges"](
        charges=[Charge(ref="t1", date="2026-08-01", amount=-1.0)]
    )
    assert out["error"]["code"] == "lading_unreachable"
    assert "pw" not in str(out)


async def test_match_charges_bad_date_is_a_validation_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    tools = build(monkeypatch, FakeReader())
    out = await tools["amazon_match_charges"](
        charges=[Charge(ref="t1", date="08/01/2026", amount=-1.0)]
    )
    assert out["error"]["code"] == "bad_date"


# ── the other tools ──────────────────────────────────────────────────


async def test_get_order_rejects_a_malformed_number(monkeypatch: pytest.MonkeyPatch) -> None:
    tools = build(monkeypatch, FakeReader())
    out = await tools["amazon_get_order"](order_number="'; DROP TABLE amazon_orders; --")
    assert out["error"]["code"] == "bad_order_number"


async def test_get_order_not_found_points_at_sync_status(monkeypatch: pytest.MonkeyPatch) -> None:
    tools = build(monkeypatch, FakeReader(orders=[]))
    out = await tools["amazon_get_order"](order_number="111-2223334-4445556")
    assert out["found"] is False
    assert "amazon_get_sync_status" in out["hint"]


async def test_list_orders_rejects_a_backwards_range(monkeypatch: pytest.MonkeyPatch) -> None:
    tools = build(monkeypatch, FakeReader())
    out = await tools["amazon_list_orders"](date_from="2026-08-04", date_to="2026-08-01")
    assert out["error"]["code"] == "bad_range"


async def test_search_items_envelope_reports_truncation(monkeypatch: pytest.MonkeyPatch) -> None:
    reader = FakeReader(
        items=[
            {
                "account": "ryan",
                "order_number": "111-A",
                "title": "Furnace Filter",
                "asin": "B0",
                "price_cents": 100,
                "quantity": 1,
                "seller": None,
                "order_placed_date": date(2026, 7, 1),
                "is_whole_foods": False,
            }
        ],
        scalar=42,
    )
    tools = build(monkeypatch, reader)
    out = await tools["amazon_search_items"](query="furnace filter")
    assert out["returned"] == 1
    assert out["total"] == 42
    assert out["truncated"] is True


async def test_sync_status_reports_coverage_and_staleness(monkeypatch: pytest.MonkeyPatch) -> None:
    reader = FakeReader(
        coverage_summary=[
            {
                "account": "ryan",
                "source": "transactions",
                "covered_from": date(2025, 1, 1),
                "covered_to": date(2026, 8, 1),
                "months": 20,
            }
        ]
    )
    tools = build(monkeypatch, reader)
    out = await tools["amazon_get_sync_status"]()
    assert out["stale"] is False
    assert out["coverage"][0]["months"] == 20
    assert out["runs"][0]["account"] == "ryan"


async def test_sync_status_goes_stale_after_the_window(monkeypatch: pytest.MonkeyPatch) -> None:
    reader = FakeReader(
        runs=[
            {
                "account": "ryan",
                "source": "transactions",
                "finished_at": datetime.now(UTC) - timedelta(hours=80),
                "status": "ok",
                "records_changed": 0,
                "parsers_pending": 0,
                "error": None,
            }
        ]
    )
    tools = build(monkeypatch, reader)
    out = await tools["amazon_get_sync_status"]()
    assert out["stale"] is True


class TestOversubscription:
    """The failure per-charge confidence cannot see.

    Found in production: two same-amount charges both came back `probable`
    against one order, and one of them was wrong. `match_charge` grades each
    charge in isolation, so the batch is the only place this is visible —
    which is the whole reason the tool takes charges in a batch.
    """

    def _entry(self, ref: str, *ids: int) -> dict[str, Any]:
        return {
            "ref": ref,
            "confidence": "probable",
            "candidates": [{"transaction_id": i, "order_number": "111-A"} for i in ids],
        }

    def test_two_charges_one_transaction_is_flagged(self) -> None:
        results = [self._entry("a", 1), self._entry("b", 1)]
        assert flag_oversubscribed(results) == 2
        assert results[0]["confidence"] == "ambiguous"
        assert results[0]["shares_with"] == ["b"]
        assert results[1]["shares_with"] == ["a"]

    def test_two_charges_two_transactions_is_legitimate(self) -> None:
        # One order really can split into two equal charges — confirmed in
        # this household's data. Both charges see both rows as candidates,
        # and there are enough rows to go round.
        results = [self._entry("a", 1, 2), self._entry("b", 1, 2)]
        assert flag_oversubscribed(results) == 0
        assert all(r["confidence"] == "probable" for r in results)
        assert all("oversubscribed" not in r for r in results)

    def test_three_charges_two_transactions_is_flagged(self) -> None:
        results = [self._entry("a", 1, 2), self._entry("b", 1, 2), self._entry("c", 1, 2)]
        assert flag_oversubscribed(results) == 3

    def test_distinct_charges_are_untouched(self) -> None:
        results = [self._entry("a", 1), self._entry("b", 2)]
        assert flag_oversubscribed(results) == 0
        assert all(r["confidence"] == "probable" for r in results)

    def test_unmatched_entries_are_ignored(self) -> None:
        results = [{"ref": "a", "confidence": "none", "candidates": []}]
        assert flag_oversubscribed(results) == 0
        assert results[0]["confidence"] == "none"


async def test_match_charges_reports_transaction_id_and_collisions(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Two ledger charges of the same amount, one Amazon transaction to go
    # round. Both used to come back `probable`; one of them is wrong.
    reader = FakeReader(transactions=[txn(id=7)], orders=[])
    tools = build(monkeypatch, reader)
    out = await tools["amazon_match_charges"](
        charges=[
            Charge(ref="t1", date="2026-08-01", amount=-84.31),
            Charge(ref="t2", date="2026-08-01", amount=-84.31),
        ]
    )
    assert out["oversubscribed"] == 2
    first = out["matches"][0]
    assert first["candidates"][0]["transaction_id"] == 7
    assert first["confidence"] == "ambiguous"
    assert first["shares_with"] == ["t2"]
