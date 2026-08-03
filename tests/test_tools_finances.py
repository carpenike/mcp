"""Finance-tool tests.

The arithmetic here is the reason this category exists in code rather than in
a prompt, so these tests pin the rules that are easy to get quietly wrong:
what counts as spend, staleness thresholds and their per-account overrides,
which side of a liability account a payment lands on, and the refusal to
invent a floor that hasn't been decided.

All upstream calls are mocked — the real sidecar is never contacted from CI.
"""

from __future__ import annotations

import json
import re
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any

import pytest
from pytest_httpx import HTTPXMock

from homelab_mcp.config import Settings
from homelab_mcp.tools.finances import register

BASE = "http://127.0.0.1:9210"

pytestmark = pytest.mark.httpx_mock(assert_all_responses_were_requested=False)


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


def _accounts() -> dict[str, Any]:
    return {
        "accounts": [
            {
                "id": "chk",
                "name": "USAA Checking",
                "offbudget": False,
                "closed": False,
                "balance_cents": 5_844_708,
            },
            {
                "id": "heloc",
                "name": "Spectra HELOC",
                "offbudget": False,
                "closed": False,
                "balance_cents": -10_929_004,
            },
            {
                "id": "apple",
                "name": "Apple Card",
                "offbudget": False,
                "closed": False,
                "balance_cents": -9_308,
            },
            {
                "id": "house",
                "name": "House",
                "offbudget": True,
                "closed": False,
                "balance_cents": 85_000_000,
            },
            # Linked as a synced off-budget account 2026-07-30; before that the
            # principal was a hand-maintained config value.
            {
                "id": "mortgage",
                "name": "Mortgage (NewRez)",
                "offbudget": True,
                "closed": False,
                "balance_cents": -39_217_246,
            },
        ]
    }


def _categories() -> dict[str, Any]:
    return {
        "categories": [
            {"id": "c1", "name": "Fixed", "group_name": "Household", "is_income": False},
            {"id": "c2", "name": "Amazon", "group_name": "Household", "is_income": False},
            {
                "id": "c3",
                "name": "CC Payments & Transfers",
                "group_name": "Household",
                "is_income": False,
            },
            {"id": "c4", "name": "Income", "group_name": "Income", "is_income": True},
        ]
    }


def _txn(**kw: Any) -> dict[str, Any]:
    base = {
        "id": "t1",
        "date": "2026-07-05",
        "amount_cents": -1000,
        "notes": None,
        "is_transfer": False,
        "account_id": "chk",
        "account_name": "USAA Checking",
        "account_offbudget": False,
        "category_id": "c1",
        "category_name": "Fixed",
        "payee_name": "Someone",
    }
    base.update(kw)
    return base


@pytest.fixture
def tools(monkeypatch: pytest.MonkeyPatch) -> dict[str, Callable[..., Any]]:
    monkeypatch.setenv("HOMELAB_MCP_OAUTH_REQUIRED", "false")
    monkeypatch.setenv("HOMELAB_MCP_FINANCES_SIDECAR_BASE_URL", BASE)
    monkeypatch.setenv("HOMELAB_MCP_FINANCES_SIDECAR_TOKEN", "tok")
    monkeypatch.setenv("HOMELAB_MCP_FINANCES_AMAZON_BASELINE", "1234.56")
    mcp = CapturingMCP()
    register(mcp, Settings())  # type: ignore[arg-type]
    return mcp.tools


def _mock_month(httpx_mock: HTTPXMock, txns: list[dict[str, Any]]) -> None:
    httpx_mock.add_response(url=f"{BASE}/categories", json=_categories())
    httpx_mock.add_response(
        url=re.compile(re.escape(BASE) + r"/transactions.*"), json={"transactions": txns}
    )


# ── spend rules ──────────────────────────────────────────────────────


async def test_monthly_summary_excludes_transfers_cc_and_offbudget(
    tools: dict[str, Callable[..., Any]], httpx_mock: HTTPXMock
) -> None:
    _mock_month(
        httpx_mock,
        [
            _txn(id="a", amount_cents=-10_000, category_name="Fixed"),
            _txn(id="b", amount_cents=-50_000, category_name="CC Payments & Transfers"),
            _txn(id="c", amount_cents=-20_000, is_transfer=True),
            _txn(id="d", amount_cents=-30_000, account_offbudget=True),
            _txn(id="e", amount_cents=500_000, category_name="Income"),
            _txn(id="f", amount_cents=-2_500, category_name="Amazon"),
        ],
    )
    out = await tools["finances_monthly_summary"](month="2026-07")
    # Only the Fixed and Amazon rows are household spend.
    assert out["total_spend"] == 125.0
    assert out["income"] == 5000.0
    assert {r["category"] for r in out["spend_by_category"]} == {"Fixed", "Amazon"}


async def test_monthly_summary_reports_amazon_week_and_mtd(
    tools: dict[str, Callable[..., Any]], httpx_mock: HTTPXMock, frozen_now: None
) -> None:
    _mock_month(
        httpx_mock,
        [
            _txn(
                id="week-a",
                date="2026-07-26",
                amount_cents=-2_500,
                category_name="Groceries & Household",
                payee_name="Amazon.com",
            ),
            _txn(
                id="mtd-a",
                date="2026-07-10",
                amount_cents=-10_000,
                category_name="Groceries & Household",
                payee_name="AMZN Mktp US",
            ),
            _txn(
                id="other",
                date="2026-07-27",
                amount_cents=-5_000,
                category_name="Groceries & Household",
                payee_name="Local Market",
            ),
        ],
    )
    out = await tools["finances_monthly_summary"](month="2026-07")
    assert out["amazon"] == {
        "week_start": "2026-07-25",
        "week_end": "2026-07-31",
        "week_spend": 25.0,
        "mtd_spend": 125.0,
        "monthly_baseline": 1234.56,
    }


async def test_monthly_summary_reports_uncategorized_separately(
    tools: dict[str, Callable[..., Any]], httpx_mock: HTTPXMock
) -> None:
    _mock_month(
        httpx_mock,
        [
            _txn(id="a", amount_cents=-10_000, category_name="Fixed"),
            _txn(id="b", amount_cents=-4_000, category_name=None, category_id=None),
        ],
    )
    out = await tools["finances_monthly_summary"](month="2026-07")
    assert out["total_spend"] == 140.0
    # Surfaced as its own signal so a caller can judge the totals' confidence.
    assert out["uncategorized"] == 40.0


async def test_gap_is_null_without_a_configured_floor(
    tools: dict[str, Callable[..., Any]], httpx_mock: HTTPXMock
) -> None:
    _mock_month(httpx_mock, [_txn(amount_cents=-10_000)])
    out = await tools["finances_monthly_summary"](month="2026-07")
    assert out["floor"] is None
    assert out["gap_vs_floor"] is None
    assert "gap_note" in out


async def test_gap_computed_when_floor_configured(
    monkeypatch: pytest.MonkeyPatch, httpx_mock: HTTPXMock
) -> None:
    monkeypatch.setenv("HOMELAB_MCP_OAUTH_REQUIRED", "false")
    monkeypatch.setenv("HOMELAB_MCP_FINANCES_SIDECAR_BASE_URL", BASE)
    monkeypatch.setenv("HOMELAB_MCP_FINANCES_FLOOR", "8400")
    mcp = CapturingMCP()
    register(mcp, Settings())  # type: ignore[arg-type]
    _mock_month(httpx_mock, [_txn(date="2026-01-05", amount_cents=-1_000_000)])
    out = await mcp.tools["finances_monthly_summary"](month="2026-01")
    assert out["gap_vs_floor"] == pytest.approx(10_000.0 - 8400.0)


async def test_bad_month_returns_structured_error(
    tools: dict[str, Callable[..., Any]],
) -> None:
    out = await tools["finances_monthly_summary"](month="July")
    assert out["error"]["code"] == "finances_bad_month"


# ── sync health vs activity ──────────────────────────────────────────


def _sync_accounts() -> dict[str, Any]:
    """Accounts carrying a `last_sync` epoch-ms, as the sidecar returns them."""
    fresh = int(datetime(2026, 7, 31, 4, 0, tzinfo=UTC).timestamp() * 1000)
    old = int(datetime(2026, 7, 20, 4, 0, tzinfo=UTC).timestamp() * 1000)
    return {
        "accounts": [
            # Healthy feed, busy account.
            {
                "id": "chk",
                "name": "USAA Checking",
                "offbudget": False,
                "closed": False,
                "balance_cents": 5_844_708,
                "last_sync": fresh,
            },
            # Healthy feed, DORMANT: no transactions for months. Must not be
            # called a sync failure — this is the defect that kept the overall
            # verdict permanently "dead".
            {
                "id": "saph",
                "name": "Chase Sapphire",
                "offbudget": False,
                "closed": False,
                "balance_cents": 9_500,
                "last_sync": fresh,
            },
            # Monthly statement feed, healthy.
            {
                "id": "apple",
                "name": "Apple Card",
                "offbudget": False,
                "closed": False,
                "balance_cents": -9_308,
                "last_sync": fresh,
            },
            # Genuinely broken feed: no fetch in 11 days.
            {
                "id": "citi",
                "name": "Citi Costco",
                "offbudget": False,
                "closed": False,
                "balance_cents": -22_473,
                "last_sync": old,
            },
            # Hand-maintained: no feed at all.
            {
                "id": "tesla",
                "name": "Tesla Loan (Santander)",
                "offbudget": True,
                "closed": False,
                "balance_cents": -4_368_588,
                "last_sync": None,
            },
        ]
    }


def _sync_cfg(tmp_path: Any) -> str:
    cfg = tmp_path / "fin.json"
    cfg.write_text(
        json.dumps(
            {
                "sync": {
                    "feed_stale_hours": 26,
                    "feed_dead_hours": 72,
                    "default_cadence": "daily",
                    "cadence_max_age_days": {"daily": 3, "monthly": 35},
                    "accounts": {
                        "USAA Checking": "daily",
                        "Chase Sapphire": "daily",
                        "Citi Costco": "daily",
                        "Apple Card": "monthly",
                        "Tesla Loan (Santander)": "manual",
                    },
                },
                "recurring": {"items": []},
            }
        )
    )
    return str(cfg)


def _mk(tmp_path: Any, **over: Any) -> dict[str, Callable[..., Any]]:
    cfg: dict[str, Any] = {
        "_env_file": None,
        "oauth_required": False,
        "finances_sidecar_base_url": BASE,
        "finances_config_path": _sync_cfg(tmp_path),
        "finances_state_path": str(tmp_path / "state.json"),
    }
    cfg.update(over)
    mcp = CapturingMCP()
    register(mcp, Settings(**cfg))  # type: ignore[arg-type]
    return mcp.tools


@pytest.fixture
def frozen_now(monkeypatch: pytest.MonkeyPatch) -> None:
    """Pin 'now' so feed ages and transaction ages are assertable."""
    import homelab_mcp.tools.finances as fin

    class FrozenDate(fin.date):  # type: ignore[misc,valid-type]
        @classmethod
        def today(cls) -> Any:
            return fin.date(2026, 7, 31)

    class FrozenDT(fin.datetime):  # type: ignore[misc,valid-type]
        @classmethod
        def now(cls, tz: Any = None) -> Any:
            return fin.datetime(2026, 7, 31, 12, 0, tzinfo=tz or UTC)

    monkeypatch.setattr(fin, "date", FrozenDate)
    monkeypatch.setattr(fin, "datetime", FrozenDT)


async def test_dormant_account_with_a_healthy_feed_is_not_stale(
    tmp_path: Any, httpx_mock: HTTPXMock, frozen_now: None
) -> None:
    """The headline defect: activity age is not sync health."""
    httpx_mock.add_response(url=f"{BASE}/accounts", json=_sync_accounts())
    httpx_mock.add_response(
        url=re.compile(re.escape(BASE) + r"/transactions.*"),
        json={
            "transactions": [
                _txn(id="1", account_id="chk", date="2026-07-30"),
                # Chase Sapphire: nothing at all in the window.
                _txn(id="2", account_id="apple", date="2026-07-05"),
                _txn(id="3", account_id="citi", date="2026-07-29"),
            ]
        },
    )
    out = await _mk(tmp_path)["finances_sync_status"]()
    rows = {r["account"]: r for r in out["accounts"]}

    saph = rows["Chase Sapphire"]
    assert saph["status"] == "fresh"  # feed fetched 8h ago
    assert saph["activity"] == "none"  # but nothing has posted
    assert saph["basis"] == "feed"
    assert "Chase Sapphire" in out["quiet_but_healthy"]
    assert "Chase Sapphire" not in out["stale_accounts"]


async def test_verdict_is_driven_by_feed_age_not_transaction_age(
    tmp_path: Any, httpx_mock: HTTPXMock, frozen_now: None
) -> None:
    httpx_mock.add_response(url=f"{BASE}/accounts", json=_sync_accounts())
    httpx_mock.add_response(
        url=re.compile(re.escape(BASE) + r"/transactions.*"),
        json={"transactions": [_txn(id="3", account_id="citi", date="2026-07-30")]},
    )
    out = await _mk(tmp_path)["finances_sync_status"]()
    rows = {r["account"]: r for r in out["accounts"]}
    # Citi posted a transaction yesterday but its FEED hasn't run in 11 days —
    # transaction age would call this healthy; it isn't.
    assert rows["Citi Costco"]["activity"] == "active"
    assert rows["Citi Costco"]["status"] == "dead"
    assert out["overall_status"] == "dead"


async def test_manual_account_is_excluded_from_the_verdict(
    tmp_path: Any, httpx_mock: HTTPXMock, frozen_now: None
) -> None:
    accounts = _sync_accounts()
    accounts["accounts"] = [a for a in accounts["accounts"] if a["id"] != "citi"]
    httpx_mock.add_response(url=f"{BASE}/accounts", json=accounts)
    httpx_mock.add_response(
        url=re.compile(re.escape(BASE) + r"/transactions.*"), json={"transactions": []}
    )
    out = await _mk(tmp_path)["finances_sync_status"]()
    assert [m["account"] for m in out["manual_accounts"]] == ["Tesla Loan (Santander)"]
    assert all(r["account"] != "Tesla Loan (Santander)" for r in out["accounts"])
    # A hand-maintained loan must never drag the verdict down.
    assert out["overall_status"] == "fresh"


async def test_missing_last_sync_falls_back_to_activity_and_says_so(
    tmp_path: Any, httpx_mock: HTTPXMock, frozen_now: None
) -> None:
    accounts = _sync_accounts()
    for a in accounts["accounts"]:
        if a["id"] == "chk":
            a["last_sync"] = None
    httpx_mock.add_response(url=f"{BASE}/accounts", json=accounts)
    httpx_mock.add_response(
        url=re.compile(re.escape(BASE) + r"/transactions.*"),
        json={"transactions": [_txn(id="1", account_id="chk", date="2026-07-30")]},
    )
    out = await _mk(tmp_path)["finances_sync_status"]()
    row = {r["account"]: r for r in out["accounts"]}["USAA Checking"]
    assert row["basis"] == "activity_fallback"
    assert "last_sync" in row["basis_note"]
    assert row["status"] == "fresh"


# ── recurring ────────────────────────────────────────────────────────


def _rec_cfg(tmp_path: Any, items: list[dict[str, Any]], **rec: Any) -> str:
    cfg = tmp_path / "rec.json"
    body: dict[str, Any] = {
        "sync": {"accounts": {"Apple Card": "monthly", "USAA Checking": "daily"}},
        "recurring": {"default_tolerance_pct": 10.0, "min_tolerance": 5.0, "items": items},
    }
    body["recurring"].update(rec)
    cfg.write_text(json.dumps(body))
    return str(cfg)


def _rec_tools(tmp_path: Any, items: list[dict[str, Any]], **rec: Any) -> Any:
    mcp = CapturingMCP()
    register(
        mcp,  # type: ignore[arg-type]
        Settings(
            _env_file=None,  # type: ignore[call-arg]
            oauth_required=False,
            finances_sidecar_base_url=BASE,
            finances_config_path=_rec_cfg(tmp_path, items, **rec),
        ),
    )
    return mcp.tools


async def test_tolerance_is_proportional_not_flat(tmp_path: Any, httpx_mock: HTTPXMock) -> None:
    """FirstEnergy at +45% used to read MATCHED against a flat +/-$300 band."""
    httpx_mock.add_response(
        url=re.compile(re.escape(BASE) + r"/transactions.*"),
        json={
            "transactions": [
                _txn(id="e", date="2026-07-16", amount_cents=-87_906, payee_name="FirstEnergy")
            ]
        },
    )
    tools = _rec_tools(
        tmp_path,
        [{"name": "Electric", "amount": 608.0, "match_any": ["firstenergy"], "ends": None}],
    )
    out = await tools["finances_recurring"](month="2026-07")
    row = out["obligations"][0]
    assert row["status"] == "CHANGED"  # 44.6% > the 10% default band
    assert row["delta"] == pytest.approx(271.06)
    assert row["delta_pct"] == pytest.approx(44.6, abs=0.1)


async def test_widened_band_still_surfaces_the_delta(tmp_path: Any, httpx_mock: HTTPXMock) -> None:
    """A seasonal override may change the STATUS but must never hide the move."""
    httpx_mock.add_response(
        url=re.compile(re.escape(BASE) + r"/transactions.*"),
        json={
            "transactions": [
                _txn(id="e", date="2026-07-16", amount_cents=-87_906, payee_name="FirstEnergy")
            ]
        },
    )
    tools = _rec_tools(
        tmp_path,
        [
            {
                "name": "Electric",
                "amount": 608.0,
                "tolerance_pct": 55.0,
                "match_any": ["firstenergy"],
                "ends": None,
            }
        ],
    )
    out = await tools["finances_recurring"](month="2026-07")
    row = out["obligations"][0]
    assert row["status"] == "MATCHED"  # inside its own widened band
    assert row["notable_variance"] is True  # but still called out
    assert [v["name"] for v in out["notable_variances"]] == ["Electric"]


async def test_monthly_account_reports_pending_statement_not_missing(
    tmp_path: Any, httpx_mock: HTTPXMock, frozen_now: None
) -> None:
    """Apple Card's installment between statement drops is not a missed payment."""
    httpx_mock.add_response(
        url=re.compile(re.escape(BASE) + r"/transactions.*"),
        json={"transactions": [_txn(id="x", account_name="USAA Checking")]},
    )
    tools = _rec_tools(
        tmp_path,
        [
            {
                "name": "MacBook installment",
                "amount": 200.33,
                "match_any": ["installment"],
                "accounts": ["Apple Card"],
                "ends": "2027-03",
            }
        ],
    )
    out = await tools["finances_recurring"](month="2026-07")
    row = out["obligations"][0]
    assert row["status"] == "PENDING_STATEMENT"
    assert "monthly statement" in row["note"]
    # PENDING is not a problem, so it must not page anyone.
    assert out["needs_attention"] == []


async def test_daily_account_with_nothing_posted_is_still_missing(
    tmp_path: Any, httpx_mock: HTTPXMock, frozen_now: None
) -> None:
    """The pending-statement escape hatch must not swallow a real miss."""
    httpx_mock.add_response(
        url=re.compile(re.escape(BASE) + r"/transactions.*"), json={"transactions": []}
    )
    tools = _rec_tools(
        tmp_path,
        [{"name": "Verizon", "amount": 95.0, "match_any": ["verizon"], "ends": None}],
    )
    out = await tools["finances_recurring"](month="2026-07")
    assert out["obligations"][0]["status"] == "MISSING"
    assert out["needs_attention"] == ["Verizon"]


async def test_recurring_reports_only_genuinely_new_payees_over_threshold(
    tmp_path: Any, httpx_mock: HTTPXMock
) -> None:
    httpx_mock.add_response(
        url=re.compile(re.escape(BASE) + r"/transactions.*"),
        json={
            "transactions": [
                _txn(
                    id="old-history",
                    date="2026-06-01",
                    amount_cents=-80_000,
                    payee_name="Existing Vendor",
                ),
                _txn(
                    id="old-current",
                    date="2026-07-12",
                    amount_cents=-70_000,
                    payee_name="Existing Vendor",
                ),
                _txn(
                    id="new-a",
                    date="2026-07-10",
                    amount_cents=-30_000,
                    payee_name="Brand New Vendor",
                ),
                _txn(
                    id="new-b",
                    date="2026-07-20",
                    amount_cents=-30_000,
                    payee_name="Brand New Vendor",
                ),
                _txn(
                    id="small",
                    date="2026-07-15",
                    amount_cents=-40_000,
                    payee_name="Small New Vendor",
                ),
                _txn(
                    id="known-bill",
                    date="2026-07-16",
                    amount_cents=-70_000,
                    payee_name="Known Bill",
                ),
            ]
        },
    )
    tools = _rec_tools(
        tmp_path,
        [{"name": "Known bill", "amount": 700.0, "match_any": ["known bill"]}],
        new_payee_threshold=500.0,
        new_payee_lookback_days=365,
    )
    out = await tools["finances_recurring"](month="2026-07")
    assert out["new_payee_threshold"] == 500.0
    assert out["new_payees_over_threshold"] == [
        {
            "payee": "Brand New Vendor",
            "spend": 600.0,
            "transaction_count": 2,
            "first_seen": "2026-07-10",
        }
    ]


async def test_recurring_matches_payment_on_the_liability_account(
    tmp_path: Any, httpx_mock: HTTPXMock
) -> None:
    """A payment toward a card/loan is a POSITIVE amount on that account."""
    httpx_mock.add_response(
        url=re.compile(re.escape(BASE) + r"/transactions.*"),
        json={
            "transactions": [
                _txn(
                    id="p",
                    date="2026-07-15",
                    amount_cents=29_200,
                    account_id="syn",
                    account_name="Synchrony Container Store",
                    payee_name="Payment",
                )
            ]
        },
    )
    tools = _rec_tools(
        tmp_path,
        [
            {
                "name": "Synchrony",
                "amount": 291.0,
                "match_any": ["synchrony"],
                "accounts": ["Synchrony Container Store"],
                "ends": None,
            }
        ],
    )
    out = await tools["finances_recurring"](month="2026-07")
    assert out["obligations"][0]["status"] == "MATCHED"
    assert out["obligations"][0]["actual_amount"] == 292.0


async def test_recurring_reports_ended_not_missing_past_end_month(
    tmp_path: Any, httpx_mock: HTTPXMock
) -> None:
    httpx_mock.add_response(
        url=re.compile(re.escape(BASE) + r"/transactions.*"), json={"transactions": []}
    )
    tools = _rec_tools(
        tmp_path,
        [{"name": "Saxophone", "amount": 97.0, "match_any": ["music"], "ends": "2026-11"}],
    )
    out = await tools["finances_recurring"](month="2026-12")
    # A finished loan must stop generating MISSING alarms forever.
    assert out["obligations"][0]["status"] == "ENDED"
    assert out["needs_attention"] == []


# ── debt ─────────────────────────────────────────────────────────────


def _debt_accounts() -> dict[str, Any]:
    return {
        "accounts": [
            {
                "id": "heloc",
                "name": "Spectra HELOC",
                "offbudget": False,
                "closed": False,
                "balance_cents": -10_929_004,
            },
            {
                "id": "mortgage",
                "name": "Mortgage (NewRez)",
                "offbudget": True,
                "closed": False,
                "balance_cents": -39_217_246,
            },
            # Off-budget AND manual — the liability that used to be invisible.
            {
                "id": "tesla",
                "name": "Tesla Loan (Santander)",
                "offbudget": True,
                "closed": False,
                "balance_cents": -4_368_588,
            },
            {
                "id": "amex",
                "name": "Amex Platinum",
                "offbudget": False,
                "closed": False,
                "balance_cents": -2_565_470,
            },
            {
                "id": "house",
                "name": "House",
                "offbudget": True,
                "closed": False,
                "balance_cents": 85_000_000,
            },
            {
                "id": "b401k",
                "name": "Microsoft 401k",
                "offbudget": True,
                "closed": False,
                "balance_cents": 79_285_250,
            },
        ]
    }


def _debt_cfg(tmp_path: Any, **over: Any) -> str:
    body: dict[str, Any] = {
        "hurdle_rate": 5.0,
        "debts": {
            "Spectra HELOC": {"rate": 6.75, "is_variable": True, "scheduled_payment": 650.0},
            "Mortgage (NewRez)": {"rate": 2.49, "is_variable": False},
            "Tesla Loan (Santander)": {"rate": 0.99, "is_variable": False},
            "Amex Platinum": {"rate": None, "is_variable": False},
        },
        "recurring": {"items": []},
    }
    body.update(over)
    cfg = tmp_path / "debt.json"
    cfg.write_text(json.dumps(body))
    return str(cfg)


def _debt_tools(tmp_path: Any, **over: Any) -> Any:
    mcp = CapturingMCP()
    register(
        mcp,  # type: ignore[arg-type]
        Settings(
            _env_file=None,  # type: ignore[call-arg]
            oauth_required=False,
            finances_sidecar_base_url=BASE,
            finances_config_path=_debt_cfg(tmp_path, **over),
            finances_state_path=str(tmp_path / "state.json"),
        ),
    )
    return mcp.tools


def _mock_debt(httpx_mock: HTTPXMock, txns: list[dict[str, Any]] | None = None) -> None:
    httpx_mock.add_response(url=f"{BASE}/accounts", json=_debt_accounts())
    httpx_mock.add_response(
        url=re.compile(re.escape(BASE) + r"/transactions.*"), json={"transactions": txns or []}
    )


async def test_debt_includes_offbudget_loans(tmp_path: Any, httpx_mock: HTTPXMock) -> None:
    """The Tesla loan was invisible because it is off-budget."""
    _mock_debt(httpx_mock)
    out = await _debt_tools(tmp_path)["finances_debt_status"]()
    names = {d["account"] for d in out["debts"]}
    assert "Tesla Loan (Santander)" in names
    assert "Mortgage (NewRez)" in names
    # Assets are never swept in, whatever their offbudget flag.
    assert "House" not in names and "Microsoft 401k" not in names
    assert out["total_debt"] == pytest.approx(-(109_290.04 + 392_172.46 + 43_685.88 + 25_654.70))


async def test_accelerate_ride_classification(tmp_path: Any, httpx_mock: HTTPXMock) -> None:
    _mock_debt(httpx_mock)
    out = await _debt_tools(tmp_path)["finances_debt_status"]()
    by = {d["account"]: d for d in out["debts"]}
    assert by["Spectra HELOC"]["class"] == "accelerate"  # 6.75 > 5.0
    assert by["Mortgage (NewRez)"]["class"] == "ride"  # 2.49 <= 5.0
    assert by["Tesla Loan (Santander)"]["class"] == "ride"  # 0.99 <= 5.0
    # An unconfigured rate is never guessed either way.
    assert by["Amex Platinum"]["class"] == "unknown"
    assert out["accelerate_total"] == pytest.approx(-109_290.04)
    assert out["ride_total"] == pytest.approx(-(392_172.46 + 43_685.88))
    assert out["unknown_total"] == pytest.approx(-25_654.70)


async def test_negative_account_absent_from_config_is_reported_not_dropped(
    tmp_path: Any, httpx_mock: HTTPXMock
) -> None:
    """Silently omitting a liability is exactly how the Tesla loan hid."""
    _mock_debt(httpx_mock)
    out = await _debt_tools(
        tmp_path,
        debts={"Spectra HELOC": {"rate": 6.75, "is_variable": True}},
    )["finances_debt_status"]()
    unlisted = {u["account"] for u in out["unlisted_negative_accounts"]}
    assert unlisted == {"Mortgage (NewRez)", "Tesla Loan (Santander)", "Amex Platinum"}
    # Still counted in the total, so it can never read smaller than reality.
    assert out["total_debt"] == pytest.approx(-(109_290.04 + 392_172.46 + 43_685.88 + 25_654.70))
    assert out["unlisted_note"] is not None


async def test_class_change_is_flagged_loudly(tmp_path: Any, httpx_mock: HTTPXMock) -> None:
    """A variable-rate debt crossing the hurdle must never pass unnoticed."""
    _mock_debt(httpx_mock)
    first = await _debt_tools(tmp_path)["finances_debt_status"]()
    assert first["class_change_detection"] == "baseline"
    assert first["class_changes"] == []

    _mock_debt(httpx_mock)
    # The HELOC is prime-pinned; rates fell and it dropped below the hurdle.
    dropped = await _debt_tools(
        tmp_path,
        debts={
            "Spectra HELOC": {"rate": 4.25, "is_variable": True},
            "Mortgage (NewRez)": {"rate": 2.49, "is_variable": False},
            "Tesla Loan (Santander)": {"rate": 0.99, "is_variable": False},
            "Amex Platinum": {"rate": None, "is_variable": False},
        },
    )["finances_debt_status"]()
    assert dropped["class_change_alert"] is True
    change = dropped["class_changes"][0]
    assert change["account"] == "Spectra HELOC"
    assert (change["was"], change["now"]) == ("accelerate", "ride")
    assert change["is_variable"] is True


async def test_starting_balance_is_not_debt_movement(tmp_path: Any, httpx_mock: HTTPXMock) -> None:
    """Linking an account must not read as taking on the whole loan this month."""
    _mock_debt(
        httpx_mock,
        [
            _txn(
                id="sb",
                date="2026-07-31",
                account_id="tesla",
                account_name="Tesla Loan (Santander)",
                amount_cents=-4_368_588,
                payee_name="Starting Balance",
                category_name=None,
            )
        ],
    )
    out = await _debt_tools(tmp_path)["finances_debt_status"]()
    tesla = {d["account"]: d for d in out["debts"]}["Tesla Loan (Santander)"]
    assert tesla["change_30d"] == 0.0
    assert tesla["change_7d"] == 0.0


async def test_equity_is_fully_derived(tmp_path: Any, httpx_mock: HTTPXMock) -> None:
    _mock_debt(httpx_mock)
    out = await _debt_tools(tmp_path)["finances_debt_status"]()
    # 850,000.00 - 392,172.46 - 109,290.04
    assert out["home_equity"] == pytest.approx(348_537.50)
    assert out["house_value"] == 850_000.0


async def test_instalment_debt_is_never_flagged_as_revolving(
    tmp_path: Any, httpx_mock: HTTPXMock
) -> None:
    _mock_debt(httpx_mock)
    out = await _debt_tools(tmp_path)["finances_debt_status"]()
    for name in ("Mortgage (NewRez)", "Spectra HELOC", "Tesla Loan (Santander)"):
        assert name not in out["cards_flagged"]


# ── error contract ───────────────────────────────────────────────────


async def test_sidecar_down_returns_structured_error_not_raise(
    tools: dict[str, Callable[..., Any]], httpx_mock: HTTPXMock
) -> None:
    import httpx

    httpx_mock.add_exception(httpx.ConnectError("refused"))
    out = await tools["finances_debt_status"]()
    assert out["error"]["code"] == "actual_unreachable"


# ── shipped config ───────────────────────────────────────────────────
# These assert the DATA in the packaged finances_config.json, not just the
# code that reads it. A nulled rate or a dropped payee form is a silent
# behavior change that no logic test would catch.


def _shipped() -> dict[str, Any]:
    import homelab_mcp.tools.finances as fin

    return dict(json.loads(fin._DEFAULT_CONFIG.read_text(encoding="utf-8")))


def test_every_shipped_debt_has_a_rate() -> None:
    """No debt may ship as 'unknown' — including the cards."""
    debts = {k: v for k, v in _shipped()["debts"].items() if not k.startswith("_")}
    missing = [k for k, v in debts.items() if v.get("rate") is None]
    assert missing == [], f"these would classify 'unknown': {missing}"


def test_every_shipped_card_is_above_the_hurdle() -> None:
    """Card APRs are the most expensive money here; none should read 'ride'."""
    cfg = _shipped()
    hurdle = cfg["hurdle_rate"]
    debts = {k: v for k, v in cfg["debts"].items() if not k.startswith("_")}
    cards = {
        k: v
        for k, v in debts.items()
        if not any(t in k.lower() for t in ("heloc", "mortgage", "loan", "synchrony"))
    }
    assert cards, "expected the card entries to be present"
    for name, meta in cards.items():
        assert meta["rate"] > hurdle, f"{name} at {meta['rate']} would classify 'ride'"


def test_usaa_life_matches_both_payee_forms() -> None:
    """The clean payee AND the raw bank descriptor, which lands in `notes`."""
    items = _shipped()["recurring"]["items"]
    item = next(i for i in items if i["name"] == "USAA life insurance")
    needles = item["match_any"]
    # Real strings observed in the budget: the descriptor carries a masked
    # account suffix, so this must stay a substring match.
    for hay in (
        "usaa life insurance ",
        "usaa life insurance usaa.com pay int life       ***********1669",
        # Casing varies between import paths.
        "USAA LIFE INSURANCE".lower(),
        "USAA.COM PAY INT LIFE  ***1669".lower(),
    ):
        assert any(n in hay for n in needles), f"no needle matched {hay!r}"


def test_usaa_life_needles_do_not_match_unrelated_payees() -> None:
    """A broader needle like 'life' would sweep in real, unrelated payees."""
    items = _shipped()["recurring"]["items"]
    needles = next(i for i in items if i["name"] == "USAA life insurance")["match_any"]
    for hay in ("lifechangers minis732 russell ave akron ", "usaa p&c insurance "):
        assert not any(n in hay for n in needles), f"false positive on {hay!r}"


async def test_recurring_matches_against_notes_not_just_payee(
    tmp_path: Any, httpx_mock: HTTPXMock
) -> None:
    """The raw descriptor lives in `notes`; matching must reach it."""
    httpx_mock.add_response(
        url=re.compile(re.escape(BASE) + r"/transactions.*"),
        json={
            "transactions": [
                _txn(
                    id="l",
                    date="2026-06-30",
                    amount_cents=-4_415,
                    payee_name="Some Opaque Payee",
                    notes="USAA.COM PAY INT LIFE       ***********1669",
                )
            ]
        },
    )
    tools = _rec_tools(
        tmp_path,
        [
            {
                "name": "USAA life",
                "amount": 44.15,
                "match_any": ["usaa life insurance", "usaa.com pay int life"],
                "ends": None,
            }
        ],
    )
    out = await tools["finances_recurring"](month="2026-06")
    row = out["obligations"][0]
    assert row["status"] == "MATCHED"
    assert row["actual_amount"] == 44.15
    # Expectation now equals the real charge, so there is no standing delta to
    # explain away every month.
    assert row["delta"] == 0.0
    assert row["notable_variance"] is False


async def test_card_balance_classifies_accelerate(tmp_path: Any, httpx_mock: HTTPXMock) -> None:
    """Any surviving card balance is the most expensive debt in the house."""
    _mock_debt(httpx_mock)
    out = await _debt_tools(
        tmp_path,
        debts={
            "Spectra HELOC": {"rate": 6.75, "is_variable": True},
            "Mortgage (NewRez)": {"rate": 2.49, "is_variable": False},
            "Tesla Loan (Santander)": {"rate": 0.99, "is_variable": False},
            "Amex Platinum": {"rate": 29.99, "is_variable": True},
        },
    )["finances_debt_status"]()
    by = {d["account"]: d for d in out["debts"]}
    assert by["Amex Platinum"]["class"] == "accelerate"
    assert out["unknown_total"] == 0.0
    assert not [d for d in out["debts"] if d["class"] == "unknown"]


def test_shipped_expectations_match_the_observed_charges() -> None:
    """Expected amounts are the real ones, so deltas mean something.

    A permanently non-zero delta is noise that trains the reader to ignore the
    column. USAA Life bills $44.15, not $44.00.
    """
    items = _shipped()["recurring"]["items"]
    by_name = {i["name"]: i for i in items}
    assert by_name["USAA life insurance"]["amount"] == 44.15
    assert by_name["Mortgage — Shellpoint/NewRez"]["amount"] == 2735.68


# ── month-boundary billing windows ───────────────────────────────────


def test_shipped_usaa_pc_expectation_matches_the_observed_series() -> None:
    """$215.47 = auto $198.89 + jewelry $16.58, observed May-Jul 2026."""
    items = _shipped()["recurring"]["items"]
    pc = next(i for i in items if i["name"] == "USAA P&C insurance")
    assert pc["amount"] == 215.47
    assert pc["amount"] == pytest.approx(198.89 + 16.58)
    assert pc["expected_day"] == 1
    # Contractually flat between repricings, so variance is signal — this one
    # must NOT get a widened seasonal band the way FirstEnergy does.
    assert "tolerance_pct" not in pc


def test_shipped_usaa_life_has_a_boundary_window() -> None:
    items = _shipped()["recurring"]["items"]
    life = next(i for i in items if i["name"] == "USAA life insurance")
    assert life["expected_day"] == 28
    assert life["window_slip_days"] == {"before": 3, "after": 4}


@pytest.mark.parametrize(
    ("month", "posted"),
    [
        ("2025-10", "2025-10-30"),  # inside the month
        ("2025-11", "2025-12-02"),  # slipped forward two days
        ("2025-12", "2025-12-30"),  # December's own, not November's
        ("2026-02", "2026-03-03"),  # slipped across a short month
        ("2026-05", "2026-06-01"),
        ("2026-06", "2026-06-30"),
    ],
)
async def test_boundary_window_credits_the_month_it_is_for(
    tmp_path: Any, httpx_mock: HTTPXMock, month: str, posted: str
) -> None:
    """The full observed USAA Life series, each posting to exactly one month."""
    series = [
        "2025-10-30",
        "2025-12-02",
        "2025-12-30",
        "2026-01-30",
        "2026-03-03",
        "2026-03-31",
        "2026-04-30",
        "2026-06-01",
        "2026-06-30",
    ]
    httpx_mock.add_response(
        url=re.compile(re.escape(BASE) + r"/transactions.*"),
        json={
            "transactions": [
                _txn(id=d, date=d, amount_cents=-4_415, payee_name="USAA Life Insurance")
                for d in series
            ]
        },
    )
    tools = _rec_tools(
        tmp_path,
        [
            {
                "name": "USAA life",
                "amount": 44.15,
                "expected_day": 28,
                "window_slip_days": {"before": 3, "after": 4},
                "match_any": ["usaa life insurance"],
                "ends": None,
            }
        ],
    )
    out = await tools["finances_recurring"](month=month)
    row = out["obligations"][0]
    assert row["status"] == "MATCHED"
    assert row["posted_date"] == posted
    assert row["delta"] == 0.0


async def test_boundary_window_never_counts_one_payment_into_two_months(
    tmp_path: Any, httpx_mock: HTTPXMock
) -> None:
    """Consecutive windows are disjoint, so no transaction is claimed twice."""
    series = ["2025-12-02", "2025-12-30", "2026-06-01", "2026-06-30"]
    item = {
        "name": "USAA life",
        "amount": 44.15,
        "expected_day": 28,
        "window_slip_days": {"before": 3, "after": 4},
        "match_any": ["usaa life insurance"],
        "ends": None,
    }
    seen: dict[str, list[str]] = {}
    for month in ("2025-11", "2025-12", "2026-05", "2026-06"):
        httpx_mock.add_response(
            url=re.compile(re.escape(BASE) + r"/transactions.*"),
            json={
                "transactions": [
                    _txn(id=d, date=d, amount_cents=-4_415, payee_name="USAA Life Insurance")
                    for d in series
                ]
            },
        )
        out = await _rec_tools(tmp_path, [item])["finances_recurring"](month=month)
        posted = out["obligations"][0].get("posted_date")
        assert posted is not None
        seen.setdefault(posted, []).append(month)
    duplicated = {d: months for d, months in seen.items() if len(months) > 1}
    assert duplicated == {}, f"transaction claimed by multiple months: {duplicated}"
    # And each month found its own distinct payment.
    assert sorted(seen) == series


async def test_slipped_match_is_labelled_as_outside_the_month(
    tmp_path: Any, httpx_mock: HTTPXMock
) -> None:
    """A reader seeing a December date under November needs to know why."""
    httpx_mock.add_response(
        url=re.compile(re.escape(BASE) + r"/transactions.*"),
        json={
            "transactions": [
                _txn(
                    id="a",
                    date="2025-12-02",
                    amount_cents=-4_415,
                    payee_name="USAA Life Insurance",
                )
            ]
        },
    )
    tools = _rec_tools(
        tmp_path,
        [
            {
                "name": "USAA life",
                "amount": 44.15,
                "expected_day": 28,
                "window_slip_days": {"before": 3, "after": 4},
                "match_any": ["usaa life insurance"],
                "ends": None,
            }
        ],
    )
    row = (await tools["finances_recurring"](month="2025-11"))["obligations"][0]
    assert row["posted_outside_month"] is True
    assert row["billing_window"] == ["2025-11-25", "2025-12-02"]


async def test_items_without_a_slip_keep_calendar_month_scope(
    tmp_path: Any, httpx_mock: HTTPXMock
) -> None:
    """The window is opt-in; a bill that posts inside its month is unaffected."""
    httpx_mock.add_response(
        url=re.compile(re.escape(BASE) + r"/transactions.*"),
        json={
            "transactions": [
                _txn(id="a", date="2026-08-01", amount_cents=-21_547, payee_name="USAA P&C")
            ]
        },
    )
    tools = _rec_tools(
        tmp_path,
        [
            {
                "name": "USAA P&C",
                "amount": 215.47,
                "expected_day": 1,
                "match_any": ["usaa p&c"],
                "ends": None,
            }
        ],
    )
    # August's charge must not be pulled back into July.
    out = await tools["finances_recurring"](month="2026-07")
    assert out["obligations"][0]["status"] == "MISSING"


# ── advisor write layer ──────────────────────────────────────────────


def _adv_tools(tmp_path: Any) -> Any:
    mcp = CapturingMCP()
    register(
        mcp,  # type: ignore[arg-type]
        Settings(
            _env_file=None,  # type: ignore[call-arg]
            oauth_required=False,
            finances_sidecar_base_url=BASE,
        ),
    )
    return mcp.tools


def _cats() -> dict[str, Any]:
    return {
        "categories": [
            {"id": "c1", "name": "Fixed", "group_name": "Household", "is_income": False},
            {"id": "c2", "name": "Amazon", "group_name": "Household", "is_income": False},
        ]
    }


# --- the structural guarantee -----------------------------------------


def test_assignment_rejects_every_field_but_category_and_notes() -> None:
    """The trust boundary, enforced by the type rather than by discipline.

    The advisor may categorize and annotate; it may never move money. These
    fields don't exist on the model, so there is no code path to them.
    """
    import pydantic

    from homelab_mcp.tools.finances import Assignment

    assert set(Assignment.model_fields) == {"transaction_id", "category", "notes"}
    for forbidden in ("amount", "payee", "payee_name", "account", "date", "cleared", "id"):
        with pytest.raises(pydantic.ValidationError):
            Assignment(transaction_id="t1", category="Fixed", **{forbidden: "x"})


def test_categorize_tool_exposes_no_money_moving_parameter(tmp_path: Any) -> None:
    """Belt-and-braces at the tool surface, not just the item model."""
    import inspect

    params = set(inspect.signature(_adv_tools(tmp_path)["finances_categorize"]).parameters)
    assert params == {"assignments"}


def test_clearing_a_category_is_distinguishable_from_omitting_it() -> None:
    """Explicit null means 'un-categorize'; omission means 'leave alone'.

    Without this an advisor could categorize but never undo, making a
    mis-categorization permanent through this interface.
    """
    from homelab_mcp.tools.finances import Assignment

    cleared = Assignment(transaction_id="t1", category=None)
    assert cleared.touches("category") is True
    assert cleared.touches("notes") is False

    notes_only = Assignment(transaction_id="t1", notes="hi")
    assert notes_only.touches("category") is False
    assert notes_only.touches("notes") is True


# --- categorize --------------------------------------------------------


async def test_categorize_sends_only_resolved_category_and_notes(
    tmp_path: Any, httpx_mock: HTTPXMock
) -> None:
    httpx_mock.add_response(url=f"{BASE}/categories", json=_cats())
    httpx_mock.add_response(
        url=f"{BASE}/transactions/categorize",
        method="POST",
        json={"results": [{"transaction_id": "t1", "ok": True}], "changed": 1},
    )
    out = await _adv_tools(tmp_path)["finances_categorize"](
        assignments=[{"transaction_id": "t1", "category": "Fixed", "notes": "n"}]
    )
    assert out["applied"] == 1
    sent = json.loads([r for r in httpx_mock.get_requests() if r.method == "POST"][0].content)[
        "assignments"
    ][0]
    # Name resolved to an id, and nothing else travels.
    assert sent == {"transaction_id": "t1", "category_id": "c1", "notes": "n"}


async def test_categorize_rejects_unknown_category_with_the_valid_list(
    tmp_path: Any, httpx_mock: HTTPXMock
) -> None:
    httpx_mock.add_response(url=f"{BASE}/categories", json=_cats())
    out = await _adv_tools(tmp_path)["finances_categorize"](
        assignments=[{"transaction_id": "t1", "category": "Nope"}]
    )
    assert out["error"]["code"] == "finances_unknown_category"
    assert "Amazon" in out["error"]["hint"] and "Fixed" in out["error"]["hint"]
    # Nothing was written.
    assert not [r for r in httpx_mock.get_requests() if r.method == "POST"]


async def test_categorize_clear_sends_explicit_null(tmp_path: Any, httpx_mock: HTTPXMock) -> None:
    httpx_mock.add_response(url=f"{BASE}/categories", json=_cats())
    httpx_mock.add_response(
        url=f"{BASE}/transactions/categorize",
        method="POST",
        json={"results": [{"transaction_id": "t1", "ok": True}], "changed": 1},
    )
    await _adv_tools(tmp_path)["finances_categorize"](
        assignments=[{"transaction_id": "t1", "category": None}]
    )
    sent = json.loads([r for r in httpx_mock.get_requests() if r.method == "POST"][0].content)[
        "assignments"
    ][0]
    assert sent == {"transaction_id": "t1", "category_id": None}


async def test_categorize_refuses_an_oversized_batch(tmp_path: Any) -> None:
    out = await _adv_tools(tmp_path)["finances_categorize"](
        assignments=[{"transaction_id": f"t{i}", "category": "Fixed"} for i in range(201)]
    )
    assert out["error"]["code"] == "finances_batch_too_large"


# --- transactions ------------------------------------------------------


def _rows() -> dict[str, Any]:
    return {
        "transactions": [
            _txn(
                id="a", date="2026-07-29", amount_cents=-2_912, payee_name="AMC", category_name=None
            ),
            _txn(
                id="b",
                date="2026-07-28",
                amount_cents=-13_183,
                payee_name="Shoes",
                category_name="Fixed",
            ),
            _txn(
                id="setup",
                date="2026-07-31",
                amount_cents=-4_368_588,
                payee_name="Starting Balance",
                category_name=None,
            ),
        ]
    }


async def test_transactions_worklist_excludes_account_setup_rows(
    tmp_path: Any, httpx_mock: HTTPXMock
) -> None:
    """Categorizing an opening balance would inject phantom spend."""
    httpx_mock.add_response(url=re.compile(re.escape(BASE) + r"/transactions.*"), json=_rows())
    out = await _adv_tools(tmp_path)["finances_transactions"](uncategorized_only=True)
    ids = [t["id"] for t in out["transactions"]]
    assert ids == ["a"]
    assert out["total"] == 1


async def test_transactions_can_opt_into_account_setup_rows(
    tmp_path: Any, httpx_mock: HTTPXMock
) -> None:
    httpx_mock.add_response(url=re.compile(re.escape(BASE) + r"/transactions.*"), json=_rows())
    out = await _adv_tools(tmp_path)["finances_transactions"](
        uncategorized_only=True, include_account_setup=True
    )
    ids = {t["id"] for t in out["transactions"]}
    assert ids == {"a", "setup"}
    assert {t["id"]: t["account_setup"] for t in out["transactions"]}["setup"] is True


async def test_transactions_reports_truncation(tmp_path: Any, httpx_mock: HTTPXMock) -> None:
    httpx_mock.add_response(url=re.compile(re.escape(BASE) + r"/transactions.*"), json=_rows())
    out = await _adv_tools(tmp_path)["finances_transactions"](limit=1)
    assert (out["returned"], out["total"], out["truncated"]) == (1, 3, True)


# --- rules -------------------------------------------------------------


def _rules_payload() -> dict[str, Any]:
    return {
        "rules": [
            {
                "id": "r1",
                "conditions": [{"op": "matches", "field": "imported_payee", "value": "AMC"}],
                "conditions_op": "and",
                "actions": [{"op": "set", "field": "category", "value": "c1"}],
                "sets_category_only": True,
            },
            {
                "id": "r2",
                "conditions": [],
                "conditions_op": "and",
                "actions": [{"op": "set", "field": "payee", "value": "p9"}],
                "sets_category_only": False,
            },
        ]
    }


async def test_rule_create_builds_a_set_category_action_only(
    tmp_path: Any, httpx_mock: HTTPXMock
) -> None:
    httpx_mock.add_response(url=f"{BASE}/categories", json=_cats())
    httpx_mock.add_response(url=f"{BASE}/rules", method="POST", json={"rule": {"id": "new"}})
    out = await _adv_tools(tmp_path)["finances_rule_create"](
        category="Fixed", imported_payee_regex="AMC|Regal"
    )
    assert out["created"] is True and out["rule_id"] == "new"
    sent = json.loads([r for r in httpx_mock.get_requests() if r.method == "POST"][0].content)
    # The caller cannot supply actions; only the category id travels.
    assert "actions" not in sent
    assert sent["category_id"] == "c1"
    assert sent["conditions"][0]["field"] == "imported_payee"


async def test_rule_create_requires_exactly_one_matcher(tmp_path: Any) -> None:
    tools = _adv_tools(tmp_path)
    both = await tools["finances_rule_create"](
        category="Fixed", payee_ids=["p1"], imported_payee_regex="x"
    )
    neither = await tools["finances_rule_create"](category="Fixed")
    assert both["error"]["code"] == "finances_rule_bad_match"
    assert neither["error"]["code"] == "finances_rule_bad_match"


async def test_rule_create_rejects_an_invalid_regex(tmp_path: Any, httpx_mock: HTTPXMock) -> None:
    out = await _adv_tools(tmp_path)["finances_rule_create"](
        category="Fixed", imported_payee_regex="([unclosed"
    )
    assert out["error"]["code"] == "finances_rule_bad_regex"


async def test_rule_delete_refuses_a_rule_that_does_more_than_categorize(
    tmp_path: Any, httpx_mock: HTTPXMock
) -> None:
    """A payee-rewriting rule must be removed where its full effect is visible."""
    httpx_mock.add_response(url=f"{BASE}/rules", method="GET", json=_rules_payload())
    out = await _adv_tools(tmp_path)["finances_rule_delete"](rule_id="r2")
    assert out["error"]["code"] == "finances_rule_not_deletable"
    assert not [r for r in httpx_mock.get_requests() if r.method == "DELETE"]


async def test_rule_delete_removes_a_set_category_rule(
    tmp_path: Any, httpx_mock: HTTPXMock
) -> None:
    httpx_mock.add_response(url=f"{BASE}/rules", method="GET", json=_rules_payload())
    httpx_mock.add_response(url=f"{BASE}/rules/r1", method="DELETE", json={"deleted": True})
    out = await _adv_tools(tmp_path)["finances_rule_delete"](rule_id="r1")
    assert out["deleted"] is True


async def test_rule_delete_unknown_id(tmp_path: Any, httpx_mock: HTTPXMock) -> None:
    httpx_mock.add_response(url=f"{BASE}/rules", method="GET", json=_rules_payload())
    out = await _adv_tools(tmp_path)["finances_rule_delete"](rule_id="nope")
    assert out["error"]["code"] == "finances_rule_not_found"


# ── buffer / breaches / room ──────────────────────────────────────────


def _v2_cfg(tmp_path: Any, _name: str = "v2.json", **over: Any) -> str:
    body: dict[str, Any] = {
        "buffer": {
            "cash_accounts": ["USAA Checking"],
            "card_accounts": ["Apple Card", "Chase Freedom", "Chase Sapphire"],
            "mortgage_obligation": "Mortgage",
            "lookahead_days": 14,
        },
        "income_payees": {"allow_substrings": ["microsoft", "irs", "interest"]},
        "lumpy": {
            "items": [
                {"name": "Baseball", "annual_amount": 2400.0, "category": "Kids"},
                {"name": "Unquantified", "annual_amount": None, "category": "Health"},
            ]
        },
        "recurring": {
            "items": [
                {"name": "Mortgage", "amount": 2735.68, "expected_day": 1, "match_any": ["m"]},
                {"name": "Verizon", "amount": 95.0, "expected_day": 20, "match_any": ["v"]},
            ]
        },
        "debts": {"Spectra HELOC": {"rate": 6.75, "is_variable": True, "scheduled_payment": 650.0}},
    }
    body.update(over)
    cfg = tmp_path / _name
    cfg.write_text(json.dumps(body))
    return str(cfg)


def _v2_tools(tmp_path: Any, **settings_over: Any) -> Any:
    kw: dict[str, Any] = {
        "_env_file": None,
        "oauth_required": False,
        "finances_sidecar_base_url": BASE,
        "finances_config_path": _v2_cfg(tmp_path),
    }
    kw.update(settings_over)
    mcp = CapturingMCP()
    register(mcp, Settings(**kw))  # type: ignore[arg-type]
    return mcp.tools


def _acct(name: str, register: int, cleared: int, **kw: Any) -> dict[str, Any]:
    base = {
        "id": name.lower().replace(" ", "-"),
        "name": name,
        "offbudget": False,
        "closed": False,
        "balance_cents": register,
        "cleared_balance_cents": cleared,
        "last_sync": "1785583758342",
        "bank_sync_status": "ok",
    }
    base.update(kw)
    return base


def _buffer_accounts() -> dict[str, Any]:
    return {
        "accounts": [
            # Register and cleared deliberately differ, to pin which is used.
            _acct("USAA Checking", 5_000_000, 4_648_201, simplefin_id="ACT-usaa"),
            _acct("Amex Savings (HYSA)", 315_993, 315_993),
            _acct("Apple Card", -9_308, -9_308),
            _acct("Chase Freedom", -10_767, 2_120),
            _acct("Chase Sapphire", 9_500, 9_500),
            _acct("Synchrony Container Store", -537_154, -537_154),
        ]
    }


def _bank_balances(available: float = 36_404.03) -> dict[str, Any]:
    return {
        "accounts": [
            {
                "simplefin_id": "ACT-usaa",
                "balance": available,
                "available_balance": available,
                "balance_date": 1785508459,
                "name": "USAA CHECKING",
                "org": "USAA",
            }
        ]
    }


async def test_buffer_uses_bank_available_cash_and_register_cards(
    tmp_path: Any, httpx_mock: HTTPXMock
) -> None:
    """Cash is what the bank says is spendable today; cards stay register."""
    httpx_mock.add_response(url=f"{BASE}/accounts", json=_buffer_accounts())
    httpx_mock.add_response(url=f"{BASE}/simplefin-balances", json=_bank_balances())
    out = await _v2_tools(tmp_path)["finances_buffer"]()
    c = out["components"]
    assert out["mode"] == "bank_available"
    # The bank's figure, NOT the 46,482.01 cleared register nor the 50,000 one.
    assert c["cash"] == 36_404.03
    assert c["cash_accounts"][0]["basis"] == "bank_available"
    assert c["cash_accounts"][0]["cleared_balance"] == 46_482.01
    # Only cards in debt: 93.08 + 107.67. Sapphire's credit is not cash.
    assert c["card_debt"] == pytest.approx(200.75)
    assert out["buffer"] == pytest.approx(36_404.03 - 200.75 - 2_735.68)
    assert "limitation" not in out


async def test_buffer_degrades_to_cleared_register_and_says_so(
    tmp_path: Any, httpx_mock: HTTPXMock
) -> None:
    """Never silently fall back — a cleared-basis buffer reads HIGH."""
    httpx_mock.add_response(url=f"{BASE}/accounts", json=_buffer_accounts())
    httpx_mock.add_response(
        url=f"{BASE}/simplefin-balances", status_code=503, json={"error": "unavailable"}
    )
    out = await _v2_tools(tmp_path)["finances_buffer"]()
    assert out["mode"] == "cleared_register"
    assert out["components"]["cash"] == 46_482.01
    assert out["components"]["cash_accounts"][0]["basis"] == "cleared_register"
    assert "may read HIGH" in out["limitation"]


async def test_buffer_lookahead_uses_the_same_basis(tmp_path: Any, httpx_mock: HTTPXMock) -> None:
    httpx_mock.add_response(url=f"{BASE}/accounts", json=_buffer_accounts())
    httpx_mock.add_response(url=f"{BASE}/simplefin-balances", json=_bank_balances())
    out = await _v2_tools(tmp_path)["finances_buffer"]()
    assert out["buffer_after_scheduled"] == pytest.approx(
        out["buffer"] - out["scheduled_outflows_total"]
    )


async def test_buffer_excludes_hysa_and_instalment_debt(
    tmp_path: Any, httpx_mock: HTTPXMock
) -> None:
    """Operations run on checking; Synchrony is a schedule, not revolving."""
    httpx_mock.add_response(url=f"{BASE}/accounts", json=_buffer_accounts())
    httpx_mock.add_response(url=f"{BASE}/simplefin-balances", json=_bank_balances())
    out = await _v2_tools(tmp_path)["finances_buffer"]()
    cash = {a["account"] for a in out["components"]["cash_accounts"]}
    cards = {a["account"] for a in out["components"]["card_accounts"]}
    assert cash == {"USAA Checking"}
    assert "Amex Savings (HYSA)" not in cash | cards
    assert "Synchrony Container Store" not in cards


async def test_buffer_reports_no_floor_until_one_is_set(
    tmp_path: Any, httpx_mock: HTTPXMock
) -> None:
    httpx_mock.add_response(url=f"{BASE}/accounts", json=_buffer_accounts())
    httpx_mock.add_response(url=f"{BASE}/simplefin-balances", json=_bank_balances())
    out = await _v2_tools(tmp_path)["finances_buffer"]()
    assert out["floor"] is None
    assert out["status"] == "no_floor"


async def test_buffer_flags_below_floor(tmp_path: Any, httpx_mock: HTTPXMock) -> None:
    httpx_mock.add_response(url=f"{BASE}/accounts", json=_buffer_accounts())
    httpx_mock.add_response(url=f"{BASE}/simplefin-balances", json=_bank_balances())
    out = await _v2_tools(tmp_path, finances_buffer_floor=90_000.0)["finances_buffer"]()
    assert out["status"] == "below_floor"


async def test_buffer_lookahead_never_exceeds_the_buffer(
    tmp_path: Any, httpx_mock: HTTPXMock
) -> None:
    """Scheduled outflows only ever reduce it — an autopay pull is not income."""
    httpx_mock.add_response(url=f"{BASE}/accounts", json=_buffer_accounts())
    httpx_mock.add_response(url=f"{BASE}/simplefin-balances", json=_bank_balances())
    out = await _v2_tools(tmp_path)["finances_buffer"]()
    assert out["buffer_after_scheduled"] <= out["buffer"]
    assert out["scheduled_outflows_total"] >= 0


async def test_breaches_flags_non_income_deposits_only(
    tmp_path: Any, httpx_mock: HTTPXMock
) -> None:
    httpx_mock.add_response(
        url=re.compile(re.escape(BASE) + r"/transactions.*"),
        json={
            "transactions": [
                # Breach: a brokerage sale funding operations.
                _txn(id="1", amount_cents=3_070_787, payee_name="Fidelity Brokerage Services"),
                # Income, allowlisted.
                _txn(id="2", amount_cents=800_000, payee_name="Microsoft Payroll"),
                # Outflow, not a deposit.
                _txn(id="3", amount_cents=-5_000, payee_name="Anything"),
                # Deposit into a non-cash account.
                _txn(id="4", amount_cents=100_000, account_name="Amex Savings (HYSA)"),
                # Account setup, not a real deposit.
                _txn(id="5", amount_cents=900_000, payee_name="Starting Balance"),
            ]
        },
    )
    out = await _v2_tools(tmp_path)["finances_breaches"](lookback_days=120)
    assert [b["payee"] for b in out["breach_candidates"]] == ["Fidelity Brokerage Services"]
    assert out["total_amount"] == pytest.approx(30_707.87)


async def test_room_arithmetic_and_savings_exclusion(
    tmp_path: Any, httpx_mock: HTTPXMock, frozen_now: None
) -> None:
    """The floor governs consumption; 529 contributions are not spend."""
    httpx_mock.add_response(url=f"{BASE}/categories", json=_categories())
    httpx_mock.add_response(
        url=re.compile(re.escape(BASE) + r"/transactions.*"),
        json={
            "transactions": [
                _txn(id="a", date="2026-07-05", amount_cents=-100_000, category_name="Fixed"),
                _txn(
                    id="b",
                    date="2026-07-06",
                    amount_cents=-50_000,
                    category_name="Savings/Investments",
                ),
            ]
        },
    )
    out = await _v2_tools(tmp_path, finances_floor=8400.0)["finances_room"]()
    assert out["consumption_mtd"] == 1000.0
    assert out["savings_mtd"] == 500.0  # reported, not counted
    # 31 July days, frozen at the 31st.
    assert out["days_elapsed"] == 31 and out["days_in_month"] == 31
    assert out["floor_to_date"] == pytest.approx(8400.0)
    assert out["pace_delta"] == pytest.approx(1000.0 - 8400.0)
    # Room is now net of the fixed bills that have not posted, so it is NOT
    # the naive 7400. Both figures are returned; the naive one is labelled.
    assert out["room_this_month_naive"] == pytest.approx(7400.0)
    assert out["room_this_month"] == pytest.approx(7400.0 - out["remaining_committed"])
    assert out["remaining_committed"] > 0


# ── amortization ──────────────────────────────────────────────────────


async def test_amortized_gap_differs_by_exactly_one_twelfth(
    tmp_path: Any, httpx_mock: HTTPXMock
) -> None:
    httpx_mock.add_response(url=f"{BASE}/categories", json=_categories())
    httpx_mock.add_response(
        url=re.compile(re.escape(BASE) + r"/transactions.*"),
        json={"transactions": [_txn(id="a", amount_cents=-900_000, category_name="Fixed")]},
    )
    out = await _v2_tools(tmp_path, finances_floor=8400.0)["finances_monthly_summary"](
        month="2026-07"
    )
    # Only the quantified lumpy contributes: 2400/12. The null one must not.
    assert out["lumpy_monthly_amortized"] == 200.0
    assert round(out["gap_vs_floor_amortized"] - out["gap_vs_floor"], 2) == 200.0
    unquantified = [i for i in out["lumpy_items"] if not i["quantified"]]
    assert len(unquantified) == 1
    assert unquantified[0]["monthly_amortized"] is None


async def test_savings_is_excluded_from_the_floor_comparator(
    tmp_path: Any, httpx_mock: HTTPXMock
) -> None:
    httpx_mock.add_response(url=f"{BASE}/categories", json=_categories())
    httpx_mock.add_response(
        url=re.compile(re.escape(BASE) + r"/transactions.*"),
        json={
            "transactions": [
                _txn(id="a", amount_cents=-100_000, category_name="Fixed"),
                _txn(id="b", amount_cents=-50_000, category_name="Savings/Investments"),
            ]
        },
    )
    out = await _v2_tools(tmp_path, finances_floor=8400.0)["finances_monthly_summary"](
        month="2026-07"
    )
    assert out["total_spend"] == 1500.0
    assert out["savings_contributions"] == 500.0
    assert out["consumption_spend"] == 1000.0
    # The gap must not penalize wealth-building.
    assert out["gap_vs_floor"] == pytest.approx(1000.0 - 8400.0)
    assert out["gap_basis"] == "consumption_spend"


# ── payoff / net worth / reconcile / payees ───────────────────────────


async def test_payoff_reports_never_when_payment_cannot_cover_interest(
    tmp_path: Any, httpx_mock: HTTPXMock
) -> None:
    httpx_mock.add_response(
        url=f"{BASE}/accounts",
        json={"accounts": [_acct("Spectra HELOC", -10_929_004, -10_929_004)]},
    )
    tools = _v2_tools(
        tmp_path,
        finances_config_path=_v2_cfg(
            tmp_path,
            "interest_only.json",
            debts={
                "Spectra HELOC": {
                    "rate": 6.75,
                    "is_variable": True,
                    # Below the ~$614.76 monthly interest.
                    "scheduled_payment": 400.0,
                }
            },
        ),
    )
    out = await tools["finances_payoff_projection"](debt_name="Spectra HELOC")
    assert out["minimum_only_baseline"]["months"] is None
    assert "never amortizes" in out["minimum_only_baseline"]["note"]


async def test_payoff_extra_payment_beats_the_baseline(
    tmp_path: Any, httpx_mock: HTTPXMock
) -> None:
    httpx_mock.add_response(
        url=f"{BASE}/accounts",
        json={"accounts": [_acct("Spectra HELOC", -10_929_004, -10_929_004)]},
    )
    out = await _v2_tools(tmp_path)["finances_payoff_projection"](
        debt_name="Spectra HELOC", extra_monthly=3000
    )
    months = out["projection"]["months"]
    assert months is not None and 30 <= months <= 40  # ~3 years, per PLAN.md
    assert out["interest_saved_vs_baseline"] > 0


async def test_payoff_refuses_to_guess_a_missing_rate(tmp_path: Any, httpx_mock: HTTPXMock) -> None:
    httpx_mock.add_response(
        url=f"{BASE}/accounts", json={"accounts": [_acct("Mystery Card", -50_000, -50_000)]}
    )
    out = await _v2_tools(tmp_path)["finances_payoff_projection"](debt_name="Mystery Card")
    assert out["error"]["code"] == "finances_rate_unknown"


async def test_net_worth_labels_equity_display_only(tmp_path: Any, httpx_mock: HTTPXMock) -> None:
    httpx_mock.add_response(
        url=f"{BASE}/accounts",
        json={
            "accounts": [
                _acct("House", 85_000_000, 85_000_000, offbudget=True),
                _acct("Mortgage (NewRez)", -39_217_246, -39_217_246, offbudget=True),
                _acct("Spectra HELOC", -10_929_004, -10_929_004),
                _acct("USAA Checking", 4_648_201, 4_648_201),
            ]
        },
    )
    out = await _v2_tools(tmp_path)["finances_net_worth"]()
    assert out["home_equity"] == pytest.approx(348_537.50)
    assert "DISPLAY ONLY" in out["home_equity_note"]
    assert out["net_worth"] == pytest.approx(850_000 - 392_172.46 - 109_290.04 + 46_482.01)


def _rc_cfg(tmp_path: Any) -> str:
    cfg = tmp_path / "rc.json"
    cfg.write_text(
        json.dumps(
            {
                "reconcile": {
                    "settlement_window_days": 7,
                    "pending_alert_threshold": 25000.0,
                    "market_valued_accounts": ["Fidelity Brokerage"],
                    "pending_inclusive_accounts": ["USAA Checking"],
                },
                "recurring": {"items": []},
            }
        )
    )
    return str(cfg)


def _rc_tools(tmp_path: Any) -> Any:
    mcp = CapturingMCP()
    register(
        mcp,  # type: ignore[arg-type]
        Settings(
            _env_file=None,  # type: ignore[call-arg]
            oauth_required=False,
            finances_sidecar_base_url=BASE,
            finances_config_path=_rc_cfg(tmp_path),
        ),
    )
    return mcp.tools


def _rc_accounts() -> dict[str, Any]:
    return {
        "accounts": [
            _acct("USAA Checking", 4_648_201, 4_648_201, simplefin_id="ACT-usaa"),
            _acct("Mortgage (NewRez)", -39_217_246, -39_217_246, simplefin_id="ACT-mtg"),
            _acct("Citi Costco", -22_473, -22_473, simplefin_id="ACT-citi"),
            _acct("Fidelity Brokerage", 13_678_711, 13_678_711, simplefin_id="ACT-fid"),
            _acct("Tesla Loan (Santander)", -4_368_588, -4_368_588, last_sync=None),
        ]
    }


def _rc_bank(usaa: float = 36_404.03) -> dict[str, Any]:
    return {
        "accounts": [
            {
                "simplefin_id": "ACT-usaa",
                "balance": usaa,
                "available_balance": usaa,
                "balance_date": 1785508459,
                "name": "USAA CHECKING",
                "org": "USAA",
            },
            {
                "simplefin_id": "ACT-mtg",
                "balance": -392_172.46,
                "available_balance": None,
                "balance_date": 1785508459,
                "name": "MTG",
                "org": "NewRez",
            },
            {
                "simplefin_id": "ACT-citi",
                "balance": 0.0,
                "available_balance": 0.0,
                "balance_date": 1785508459,
                "name": "CITI",
                "org": "Citi",
            },
            {
                "simplefin_id": "ACT-fid",
                "balance": 156_704.40,
                "available_balance": None,
                "balance_date": 1785508459,
                "name": "FID",
                "org": "Fidelity",
            },
        ]
    }


def _rc_mock(httpx_mock: HTTPXMock, bank: dict[str, Any], activity_cents: int) -> None:
    httpx_mock.add_response(url=f"{BASE}/accounts", json=_rc_accounts())
    httpx_mock.add_response(url=f"{BASE}/simplefin-balances", json=bank)
    httpx_mock.add_response(
        url=re.compile(re.escape(BASE) + r"/transactions.*"),
        json={
            "transactions": [_txn(id="r", account_id="usaa-checking", amount_cents=activity_cents)]
        },
    )


async def test_reconcile_uses_the_banks_own_balance(tmp_path: Any, httpx_mock: HTTPXMock) -> None:
    """The whole point: a register can now be checked against reality."""
    _rc_mock(httpx_mock, _rc_bank(), -9_472_897)  # big recent activity
    out = await _rc_tools(tmp_path)["finances_reconcile"]()
    assert out["mode"] == "bank_verified"
    by = {r["account"]: r for r in out["accounts"]}
    assert by["USAA Checking"]["bank_reported_balance"] == 36_404.03
    assert by["USAA Checking"]["drift"] == pytest.approx(10_077.98)


async def test_reconcile_classifications(tmp_path: Any, httpx_mock: HTTPXMock) -> None:
    _rc_mock(httpx_mock, _rc_bank(), -9_472_897)
    out = await _rc_tools(tmp_path)["finances_reconcile"]()
    by = {r["account"]: r for r in out["accounts"]}
    # Bank agrees exactly.
    assert by["Mortgage (NewRez)"]["classification"] == "exact"
    # Drift smaller than the week's activity is float, not fault.
    assert by["USAA Checking"]["classification"] == "settlement_window"
    assert by["USAA Checking"]["pending_inclusive"] is True
    # Live market value vs a transaction-driven register.
    assert by["Fidelity Brokerage"]["classification"] == "market_movement"
    # No bank to disagree with.
    assert by["Tesla Loan (Santander)"]["classification"] == "manual"
    assert by["Tesla Loan (Santander)"]["drift"] is None
    assert "Tesla Loan (Santander)" not in out["needs_attention"]


async def test_reconcile_detects_a_structural_drift(tmp_path: Any, httpx_mock: HTTPXMock) -> None:
    """The Jan-Apr 2026 fault: register wrong for months, everything cleared.

    Perturb the register far beyond the settlement window with almost no
    recent activity — the shape cleared-vs-register mode could never see.
    """
    accounts = _rc_accounts()
    for a in accounts["accounts"]:
        if a["name"] == "USAA Checking":
            # $12,195.50 off, exactly the documented anchor error, and fully
            # "cleared" so the old mode reported it as healthy.
            a["balance_cents"] = a["cleared_balance_cents"] = 4_859_953
    httpx_mock.add_response(url=f"{BASE}/accounts", json=accounts)
    httpx_mock.add_response(url=f"{BASE}/simplefin-balances", json=_rc_bank(36_404.03))
    httpx_mock.add_response(
        url=re.compile(re.escape(BASE) + r"/transactions.*"),
        json={"transactions": [_txn(id="q", account_id="usaa-checking", amount_cents=-5_000)]},
    )
    out = await _rc_tools(tmp_path)["finances_reconcile"]()
    usaa = {r["account"]: r for r in out["accounts"]}["USAA Checking"]
    assert usaa["classification"] == "structural"
    assert usaa["uncleared"] == 0.0  # fully cleared, yet wrong
    assert "anchor-error shape" in usaa["note"]
    assert "USAA Checking" in out["needs_attention"]


async def test_pending_inclusive_does_not_license_unlimited_drift(
    tmp_path: Any, httpx_mock: HTTPXMock
) -> None:
    """The flag explains expected drift; it must not suppress a huge one."""
    _rc_mock(httpx_mock, _rc_bank(usaa=-30_000.0), -99_999_999)
    out = await _rc_tools(tmp_path)["finances_reconcile"]()
    usaa = {r["account"]: r for r in out["accounts"]}["USAA Checking"]
    assert usaa["drift"] > 25_000.0
    assert usaa["classification"] == "structural"
    assert "alert threshold" in usaa["note"]


async def test_reconcile_degrades_honestly_without_bank_balances(
    tmp_path: Any, httpx_mock: HTTPXMock
) -> None:
    """Never report a comparison it did not make."""
    httpx_mock.add_response(url=f"{BASE}/accounts", json=_rc_accounts())
    httpx_mock.add_response(
        url=f"{BASE}/simplefin-balances", status_code=503, json={"error": "simplefin_unavailable"}
    )
    httpx_mock.add_response(
        url=re.compile(re.escape(BASE) + r"/transactions.*"), json={"transactions": []}
    )
    out = await _rc_tools(tmp_path)["finances_reconcile"]()
    assert out["mode"] == "cleared_only"
    assert "would be invisible" in out["limitation"]
    by = {r["account"]: r for r in out["accounts"]}
    assert by["USAA Checking"]["classification"] == "no_bank_balance"
    assert "USAA Checking" in out["needs_attention"]


async def test_payee_merge_refuses_a_transfer_payee(tmp_path: Any, httpx_mock: HTTPXMock) -> None:
    """Merging a transfer payee would corrupt account-to-account wiring."""
    httpx_mock.add_response(
        url=f"{BASE}/payees",
        json={
            "payees": [
                {"id": "keep", "name": "Costco", "transaction_count": 10, "transfer_acct": None},
                {
                    "id": "xfer",
                    "name": "Transfer: HYSA",
                    "transaction_count": 3,
                    "transfer_acct": "a1",
                },
            ]
        },
    )
    out = await _v2_tools(tmp_path)["finances_payee_merge"](keep_id="keep", merge_ids=["xfer"])
    assert out["error"]["code"] == "finances_merge_transfer_payee"
    assert not [r for r in httpx_mock.get_requests() if r.method == "POST"]


async def test_payee_merge_reports_irreversibility(tmp_path: Any, httpx_mock: HTTPXMock) -> None:
    httpx_mock.add_response(
        url=f"{BASE}/payees",
        json={
            "payees": [
                {"id": "keep", "name": "Costco", "transaction_count": 10, "transfer_acct": None},
                {"id": "dup", "name": "COSTCO WHSE", "transaction_count": 4, "transfer_acct": None},
            ]
        },
    )
    httpx_mock.add_response(url=f"{BASE}/payees/merge", method="POST", json={"merged": True})
    out = await _v2_tools(tmp_path)["finances_payee_merge"](keep_id="keep", merge_ids=["dup"])
    assert out["merged"] is True
    assert out["reversible"] is False
    assert out["transactions_repointed"] == 4


async def test_subscriptions_requires_min_months(tmp_path: Any, httpx_mock: HTTPXMock) -> None:
    httpx_mock.add_response(url=f"{BASE}/categories", json=_categories())
    httpx_mock.add_response(
        url=re.compile(re.escape(BASE) + r"/transactions.*"),
        json={
            "transactions": [
                _txn(id=f"s{i}", date=f"2026-0{i}-10", amount_cents=-1_099, payee_name="Spotify")
                for i in range(1, 5)
            ]
            + [_txn(id="one", date="2026-05-01", amount_cents=-9_999, payee_name="One Off")]
        },
    )
    out = await _v2_tools(tmp_path)["finances_subscriptions"](min_months=3)
    names = {s["payee"] for s in out["subscriptions"]}
    assert names == {"Spotify"}
    spotify = out["subscriptions"][0]
    assert spotify["months_present"] == 4
    assert spotify["fixed_amount"] is True
    assert spotify["first_seen"] == "2026-01-10"


# ── room: committed-bill correction ──────────────────────────────────


def _room_cfg(tmp_path: Any) -> str:
    """Two fixed bills: one on the 1st, one mid-month."""
    cfg = tmp_path / "room.json"
    cfg.write_text(
        json.dumps(
            {
                "buffer": {"cash_accounts": ["USAA Checking"], "card_accounts": []},
                "recurring": {
                    "default_tolerance_pct": 10.0,
                    "min_tolerance": 5.0,
                    "items": [
                        {
                            "name": "Mortgage",
                            "amount": 2000.0,
                            "expected_day": 1,
                            "match_any": ["shellpoint"],
                            "ends": None,
                        },
                        {
                            "name": "Tesla",
                            "amount": 600.0,
                            "expected_day": 15,
                            "match_any": ["santander"],
                            "ends": None,
                        },
                        # Debt service on a liability account: posts as a
                        # transfer, so it never touches the consumption floor.
                        {
                            "name": "Synchrony",
                            "amount": 300.0,
                            "expected_day": 15,
                            "match_any": ["synchrony"],
                            "accounts": ["Synchrony Container Store"],
                            "ends": None,
                        },
                    ],
                },
            }
        )
    )
    return str(cfg)


def _room_tools(tmp_path: Any, **over: Any) -> Any:
    kw: dict[str, Any] = {
        "_env_file": None,
        "oauth_required": False,
        "finances_sidecar_base_url": BASE,
        "finances_config_path": _room_cfg(tmp_path),
        "finances_floor": 8400.0,
    }
    kw.update(over)
    mcp = CapturingMCP()
    register(mcp, Settings(**kw))  # type: ignore[arg-type]
    return mcp.tools


def _room_mock(httpx_mock: HTTPXMock, txns: list[dict[str, Any]]) -> None:
    httpx_mock.add_response(url=f"{BASE}/categories", json=_categories())
    httpx_mock.add_response(
        url=re.compile(re.escape(BASE) + r"/transactions.*"), json={"transactions": txns}
    )


async def test_room_is_net_of_bills_not_yet_posted(
    tmp_path: Any, httpx_mock: HTTPXMock, frozen_now: None
) -> None:
    """The headline defect: naive floor-minus-spend overstates room early."""
    # Nothing posted yet. Both floor-bearing bills are still to come.
    _room_mock(httpx_mock, [])
    out = await _room_tools(tmp_path)["finances_room"]()
    assert out["remaining_committed"] == 2600.0  # 2000 + 600, NOT the Synchrony 300
    assert out["room_this_month"] == pytest.approx(8400.0 - 0.0 - 2600.0)
    assert out["room_this_month_naive"] == 8400.0
    assert out["room_this_month"] < out["room_this_month_naive"]


async def test_room_committed_items_are_itemized(
    tmp_path: Any, httpx_mock: HTTPXMock, frozen_now: None
) -> None:
    """Callers must be able to say '$X after $Y of upcoming bills'."""
    _room_mock(httpx_mock, [])
    out = await _room_tools(tmp_path)["finances_room"]()
    names = [c["name"] for c in out["remaining_committed_items"]]
    assert names == ["Mortgage", "Tesla"]
    assert sum(c["expected_amount"] for c in out["remaining_committed_items"]) == 2600.0
    assert "never the naive" in out["presentation_note"]


async def test_room_drops_a_bill_from_committed_once_it_posts(
    tmp_path: Any, httpx_mock: HTTPXMock, frozen_now: None
) -> None:
    _room_mock(
        httpx_mock,
        [
            _txn(
                id="m",
                date="2026-07-01",
                amount_cents=-200_000,
                payee_name="Shellpoint Mortgage",
                category_name="Fixed",
            )
        ],
    )
    out = await _room_tools(tmp_path)["finances_room"]()
    assert [c["name"] for c in out["remaining_committed_items"]] == ["Tesla"]
    assert out["remaining_committed"] == 600.0
    assert out["fixed_posted"] == 2000.0
    # 8400 - 2000 spent - 600 still to come.
    assert out["room_this_month"] == pytest.approx(5800.0)


@pytest.fixture
def frozen_early(monkeypatch: pytest.MonkeyPatch) -> None:
    """Day 2 of the month — where the 1st-of-month mortgage distorts pace."""
    import homelab_mcp.tools.finances as fin

    class D(fin.date):  # type: ignore[misc,valid-type]
        @classmethod
        def today(cls) -> Any:
            return fin.date(2026, 7, 2)

    class DT(fin.datetime):  # type: ignore[misc,valid-type]
        @classmethod
        def now(cls, tz: Any = None) -> Any:
            return fin.datetime(2026, 7, 2, 12, 0, tzinfo=tz or UTC)

    monkeypatch.setattr(fin, "date", D)
    monkeypatch.setattr(fin, "datetime", DT)


async def test_pace_is_measured_on_variable_spend_only(
    tmp_path: Any, httpx_mock: HTTPXMock, frozen_early: None
) -> None:
    """Pacing on the total makes the 1st-of-month mortgage a false spike."""
    _room_mock(
        httpx_mock,
        [
            _txn(
                id="m",
                date="2026-07-01",
                amount_cents=-200_000,
                payee_name="Shellpoint Mortgage",
                category_name="Fixed",
            ),
            _txn(id="v", date="2026-07-02", amount_cents=-10_000, category_name="Amazon"),
        ],
    )
    out = await _room_tools(tmp_path)["finances_room"]()
    assert out["consumption_mtd"] == 2100.0
    assert out["fixed_posted"] == 2000.0
    assert out["variable_mtd"] == 100.0
    # variable floor = 8400 - (2000 + 600) = 5800; the Synchrony 300 is excluded.
    assert out["variable_floor"] == 5800.0
    # Day 2 of 31. Variable pacing says fine: $100 spent against $374 allowed.
    assert out["variable_floor_to_date"] == pytest.approx(round(5800.0 * 2 / 31, 2))
    assert out["variable_pace_delta"] < 0
    assert out["pace"] == "on_or_under"
    # Total-based pacing on the same day screams overspend purely because the
    # mortgage landed on the 1st. THIS is why pace is measured on variable.
    assert out["pace_delta"] > 0
    assert out["pace_delta"] > out["variable_pace_delta"]


async def test_room_excludes_debt_service_legs_from_the_floor(
    tmp_path: Any, httpx_mock: HTTPXMock, frozen_now: None
) -> None:
    """Synchrony posts as a transfer; it never touches consumption spend."""
    _room_mock(httpx_mock, [])
    out = await _room_tools(tmp_path)["finances_room"]()
    assert out["obligations_excluded_from_floor"] == ["Synchrony"]
    assert out["fixed_expected_total"] == 2600.0


async def test_room_and_recurring_agree_on_what_has_posted(
    tmp_path: Any, httpx_mock: HTTPXMock, frozen_now: None
) -> None:
    """Both call the same matcher, so their views can never diverge."""
    txns = [
        _txn(
            id="m",
            date="2026-07-01",
            amount_cents=-200_000,
            payee_name="Shellpoint Mortgage",
            category_name="Fixed",
        )
    ]
    _room_mock(httpx_mock, txns)
    tools = _room_tools(tmp_path)
    room = await tools["finances_room"]()
    httpx_mock.add_response(
        url=re.compile(re.escape(BASE) + r"/transactions.*"), json={"transactions": txns}
    )
    rec = await tools["finances_recurring"](month="2026-07")
    posted = {o["name"] for o in rec["obligations"] if o["status"] in ("MATCHED", "CHANGED")}
    still_due = {c["name"] for c in room["remaining_committed_items"]}
    assert "Mortgage" in posted
    assert "Mortgage" not in still_due


async def test_room_without_a_floor_reports_nulls_not_guesses(
    tmp_path: Any, httpx_mock: HTTPXMock, frozen_now: None
) -> None:
    _room_mock(httpx_mock, [])
    out = await _room_tools(tmp_path, finances_floor=None)["finances_room"]()
    for key in ("room_this_month", "variable_floor", "variable_pace_delta"):
        assert out[key] is None
    # Committed bills are still computable and still reported.
    assert out["remaining_committed"] == 2600.0


# ── room: glide / recovery math ──────────────────────────────────────
# PULSE.md's tone rule is "recovery math, never a bare verdict", so these
# pin the arithmetic a caller would otherwise have to do itself.


@pytest.fixture
def frozen_midmonth(monkeypatch: pytest.MonkeyPatch) -> None:
    """Day 21 of 31 — the week-3 over-pace moment this exists for."""
    import homelab_mcp.tools.finances as fin

    class D(fin.date):  # type: ignore[misc,valid-type]
        @classmethod
        def today(cls) -> Any:
            return fin.date(2026, 7, 21)

    class DT(fin.datetime):  # type: ignore[misc,valid-type]
        @classmethod
        def now(cls, tz: Any = None) -> Any:
            return fin.datetime(2026, 7, 21, 12, 0, tzinfo=tz or UTC)

    monkeypatch.setattr(fin, "date", D)
    monkeypatch.setattr(fin, "datetime", DT)


async def test_required_pace_hand_calc_when_over_pace(
    tmp_path: Any, httpx_mock: HTTPXMock, frozen_midmonth: None
) -> None:
    """Day 21/31: $6,000 spent, both bills paid, floor 8400."""
    _room_mock(
        httpx_mock,
        [
            _txn(
                id="m",
                date="2026-07-01",
                amount_cents=-200_000,
                payee_name="Shellpoint Mortgage",
                category_name="Fixed",
            ),
            _txn(
                id="t",
                date="2026-07-15",
                amount_cents=-60_000,
                payee_name="Santander",
                category_name="Fixed",
            ),
            _txn(id="v", date="2026-07-10", amount_cents=-340_000, category_name="Amazon"),
        ],
    )
    out = await _room_tools(tmp_path)["finances_room"]()
    assert out["consumption_mtd"] == 6000.0
    assert out["remaining_committed"] == 0.0  # both floor-bearing bills posted
    assert out["room_this_month"] == pytest.approx(2400.0)  # 8400 - 6000 - 0
    assert out["days_remaining"] == 10
    # 2400 / 10 days
    assert out["required_remaining_pace"] == pytest.approx(240.0)


async def test_required_pace_is_negative_when_room_is_negative(
    tmp_path: Any, httpx_mock: HTTPXMock, frozen_midmonth: None
) -> None:
    """A blown floor must be reported as a negative, not clamped to zero.

    Clamping would turn the number into the bare verdict it replaces.
    """
    _room_mock(
        httpx_mock,
        [_txn(id="v", date="2026-07-10", amount_cents=-1_000_000, category_name="Amazon")],
    )
    out = await _room_tools(tmp_path)["finances_room"]()
    # 8400 - 10000 - 2600 still committed.
    assert out["room_this_month"] == pytest.approx(-4200.0)
    assert out["required_remaining_pace"] == pytest.approx(-420.0)
    assert out["required_remaining_pace"] < 0


async def test_recovery_delta_relates_typical_to_required(
    tmp_path: Any, httpx_mock: HTTPXMock, frozen_midmonth: None
) -> None:
    """recovery_delta = typical − required, on the same variable basis."""
    prior = [
        # A prior month with $3,100 of variable spend and its fixed bills paid.
        _txn(
            id="pm",
            date="2026-06-01",
            amount_cents=-200_000,
            payee_name="Shellpoint Mortgage",
            category_name="Fixed",
        ),
        _txn(id="pv", date="2026-06-10", amount_cents=-310_000, category_name="Amazon"),
    ]
    _room_mock(
        httpx_mock,
        [*prior, _txn(id="v", date="2026-07-10", amount_cents=-340_000, category_name="Amazon")],
    )
    out = await _room_tools(tmp_path)["finances_room"]()
    assert out["trailing_months_used"] >= 1
    typical = out["typical_remaining_pace"]
    required = out["required_remaining_pace"]
    assert typical is not None
    # June: 3,100 variable over 30 days.
    assert typical == pytest.approx(103.33, abs=0.02)
    assert out["recovery_delta"] == pytest.approx(round(typical - required, 2))


async def test_last_day_of_month_does_not_divide_by_zero(
    tmp_path: Any, httpx_mock: HTTPXMock, frozen_now: None
) -> None:
    """Day 31/31: days_remaining is 0; the divisor floors at 1."""
    _room_mock(httpx_mock, [])
    out = await _room_tools(tmp_path)["finances_room"]()
    assert out["days_remaining"] == 0
    # room / 1, not a ZeroDivisionError and not an infinity.
    assert out["required_remaining_pace"] == pytest.approx(out["room_this_month"])
    assert "floors at 1 day" in out["recovery_basis"]


async def test_glide_fields_are_null_without_a_floor(
    tmp_path: Any, httpx_mock: HTTPXMock, frozen_midmonth: None
) -> None:
    _room_mock(httpx_mock, [])
    out = await _room_tools(tmp_path, finances_floor=None)["finances_room"]()
    assert out["required_remaining_pace"] is None
    assert out["recovery_delta"] is None
    # Typical pace needs no floor, so it is still reported.
    assert "typical_remaining_pace" in out


async def test_glide_fields_are_additive(
    tmp_path: Any, httpx_mock: HTTPXMock, frozen_midmonth: None
) -> None:
    """Existing consumers must be unaffected."""
    _room_mock(httpx_mock, [])
    out = await _room_tools(tmp_path)["finances_room"]()
    for key in (
        "room_this_month",
        "room_this_month_naive",
        "remaining_committed",
        "variable_floor",
        "variable_mtd",
        "variable_pace_delta",
        "pace",
        "consumption_mtd",
        "savings_mtd",
    ):
        assert key in out, f"pre-existing field {key} disappeared"


# ── employer concentration ───────────────────────────────────────────


def _conc_accounts() -> dict[str, Any]:
    return {
        "accounts": [
            _acct("USAA Checking", 4_648_201, 4_648_201),
            # The largest account by far — and a target-date fund, not stock.
            _acct("Microsoft 401k", 79_285_250, 79_285_250, offbudget=True),
            _acct("Fidelity Brokerage", 13_678_711, 13_678_711, offbudget=True),
            _acct("Microsoft ESPP", 224_315, 224_315, offbudget=True),
            _acct("Fidelity HSA", 4_407_512, 4_407_512, offbudget=True),
            _acct("House", 85_000_000, 85_000_000, offbudget=True),
            _acct("Mortgage (NewRez)", -39_217_246, -39_217_246, offbudget=True),
        ]
    }


def _conc_cfg(tmp_path: Any, msft: float | None = 137_000.0, name: str = "conc.json") -> str:
    cfg = tmp_path / name
    cfg.write_text(
        json.dumps(
            {
                "buffer": {"cash_accounts": ["USAA Checking"], "card_accounts": []},
                "recurring": {"items": []},
                "debts": {},
                "concentration": {
                    "employer_tied_accounts": ["Microsoft ESPP"],
                    "msft_shares_in_brokerage": msft,
                    "msft_shares_source_account": "Fidelity Brokerage",
                    "single_employer_income": True,
                },
            }
        )
    )
    return str(cfg)


def _conc_tools(tmp_path: Any, **over: Any) -> Any:
    mcp = CapturingMCP()
    register(
        mcp,  # type: ignore[arg-type]
        Settings(
            _env_file=None,  # type: ignore[call-arg]
            oauth_required=False,
            finances_sidecar_base_url=BASE,
            finances_config_path=_conc_cfg(tmp_path, **over),
        ),
    )
    return mcp.tools


def _conc_mock(httpx_mock: HTTPXMock) -> None:
    httpx_mock.add_response(url=f"{BASE}/accounts", json=_conc_accounts())
    httpx_mock.add_response(
        url=re.compile(re.escape(BASE) + r"/transactions.*"), json={"transactions": []}
    )


async def test_concentration_excludes_the_401k(tmp_path: Any, httpx_mock: HTTPXMock) -> None:
    """The 401(k) is a target-date fund, not employer stock.

    It is the largest single account, so counting it would roughly quintuple
    the reported figure and make the metric worse than useless.
    """
    _conc_mock(httpx_mock)
    out = await _conc_tools(tmp_path)["finances_net_worth"]()
    conc = out["concentration"]
    named = {c["account"] for c in conc["components"]}
    assert "Microsoft 401k" not in named
    assert conc["employer_tied_assets"] == pytest.approx(137_000.0 + 2_243.15)
    # And the exclusion is stated, not merely implied by absence.
    assert any(e["account"] == "Microsoft 401k" for e in conc["excluded"])


async def test_concentration_percentage(tmp_path: Any, httpx_mock: HTTPXMock) -> None:
    _conc_mock(httpx_mock)
    out = await _conc_tools(tmp_path)["finances_net_worth"]()
    conc = out["concentration"]
    # investable = 401k + brokerage + ESPP + HSA (house and cash excluded)
    investable = 792_852.50 + 136_787.11 + 2_243.15 + 44_075.12
    assert conc["investable_total"] == pytest.approx(investable)
    assert conc["concentration_pct"] == pytest.approx(
        round((137_000.0 + 2_243.15) / investable * 100.0, 1)
    )
    assert conc["concentration_pct_status"] == "measured"


async def test_unset_brokerage_value_reports_unknown_not_zero(
    tmp_path: Any, httpx_mock: HTTPXMock
) -> None:
    """A falsely-low percentage is worse than an honest 'unmeasured'."""
    _conc_mock(httpx_mock)
    out = await _conc_tools(tmp_path, msft=None, name="conc_null.json")["finances_net_worth"]()
    conc = out["concentration"]
    assert conc["concentration_pct"] is None
    assert conc["concentration_pct_status"] == "unknown"
    assert "not as low" in conc["unknown_reason"]
    # The ESPP figure is real and still shown; it is just partial.
    assert conc["employer_tied_assets"] == pytest.approx(2_243.15)
    assert [c["account"] for c in conc["components"]] == ["Microsoft ESPP"]


async def test_income_concentration_is_a_separate_fact(
    tmp_path: Any, httpx_mock: HTTPXMock
) -> None:
    _conc_mock(httpx_mock)
    out = await _conc_tools(tmp_path)["finances_net_worth"]()
    assert out["concentration"]["single_employer_income"] is True


async def test_concentration_components_record_their_source(
    tmp_path: Any, httpx_mock: HTTPXMock
) -> None:
    """One value is live, one is hand-maintained — the reader should know."""
    _conc_mock(httpx_mock)
    conc = (await _conc_tools(tmp_path)["finances_net_worth"]())["concentration"]
    by = {c["account"]: c for c in conc["components"]}
    assert by["Microsoft ESPP"]["source"] == "actual"
    assert by["Fidelity Brokerage"]["source"] == "config"
    assert "monthly reviews" in by["Fidelity Brokerage"]["note"]


async def test_net_worth_fields_are_additive(tmp_path: Any, httpx_mock: HTTPXMock) -> None:
    _conc_mock(httpx_mock)
    out = await _conc_tools(tmp_path)["finances_net_worth"]()
    for key in (
        "net_worth",
        "assets_total",
        "debts_total",
        "cash",
        "investable",
        "property",
        "home_equity",
        "home_equity_note",
        "assets",
        "debts",
    ):
        assert key in out, f"pre-existing field {key} disappeared"


# ── transaction context ──────────────────────────────────────────────


def _ctx_tools(tmp_path: Any, limit: int = 20) -> Any:
    mcp = CapturingMCP()
    register(
        mcp,  # type: ignore[arg-type]
        Settings(
            _env_file=None,  # type: ignore[call-arg]
            oauth_required=False,
            finances_sidecar_base_url=BASE,
            finances_config_path=_v2_cfg(tmp_path),
            finances_context_db_path=str(tmp_path / "ctx.db"),
            finances_context_daily_limit=limit,
        ),
    )
    return mcp.tools


async def test_context_add_list_consume_round_trip(tmp_path: Any) -> None:
    tools = _ctx_tools(tmp_path)
    added = await tools["finances_context_add"](
        note="Jayme's birthday dinner — the whole family went.",
        author="+1240...",
        source="pulse_clarify",
        ref_date="2026-07-27",
        ref_amount=266.14,
        ref_payee="Bavarian Inn",
    )
    assert added["added"] is True
    entry = added["entry"]
    assert entry["status"] == "open"
    # Stored verbatim — not summarized into a category.
    assert entry["note"].startswith("Jayme's birthday dinner")
    assert entry["txn_ref"] == {
        "date": "2026-07-27",
        "amount": 266.14,
        "payee_hint": "Bavarian Inn",
    }

    listed = await tools["finances_context_list"]()
    assert [e["id"] for e in listed["entries"]] == [entry["id"]]

    done = await tools["finances_context_consume"](
        ids=[entry["id"]], note="Categorized as Dining & Fun."
    )
    assert done["consumed"] == [entry["id"]]
    # Consuming removes it from the open queue, so nobody is re-asked.
    assert (await tools["finances_context_list"]())["entries"] == []
    closed = await tools["finances_context_list"](status="consumed")
    assert closed["entries"][0]["consumed_by"] == "advisor"
    assert "Categorized as Dining & Fun." in closed["entries"][0]["note"]


async def test_context_stores_a_statement_with_no_posted_transaction(
    tmp_path: Any,
) -> None:
    """The pre-posting case — the whole reason txn_ref is a hint, not an id.

    Someone says "I just bought X" before the card feed has it. That sentence
    is available for about a day; the transaction may take three.
    """
    tools = _ctx_tools(tmp_path)
    added = await tools["finances_context_add"](
        note="Just ordered the mixer — this is the queued WANT, not an impulse.",
        author="ryan",
        source="volunteered",
    )
    assert added["added"] is True
    assert added["entry"]["txn_ref"] == {"date": None, "amount": None, "payee_hint": None}
    listed = await tools["finances_context_list"]()
    assert listed["entries"][0]["id"] == added["entry"]["id"]


async def test_consume_reports_ids_it_did_not_change(tmp_path: Any) -> None:
    """Silently ignoring a bad id would hide a caller's mistake."""
    tools = _ctx_tools(tmp_path)
    e = (await tools["finances_context_add"](note="x", author="a"))["entry"]
    await tools["finances_context_consume"](ids=[e["id"]])
    again = await tools["finances_context_consume"](ids=[e["id"], 9999])
    assert again["consumed"] == []
    assert again["already_closed"] == [e["id"]]
    assert again["not_found"] == [9999]


async def test_rate_limit_is_enforced_per_author(tmp_path: Any) -> None:
    tools = _ctx_tools(tmp_path, limit=3)
    for i in range(3):
        assert (await tools["finances_context_add"](note=f"n{i}", author="loop"))["added"]
    blocked = await tools["finances_context_add"](note="one too many", author="loop")
    assert blocked["error"]["code"] == "finances_context_rate_limited"
    # Per-author: someone else is unaffected.
    assert (await tools["finances_context_add"](note="fine", author="other"))["added"]


async def test_remaining_today_counts_down(tmp_path: Any) -> None:
    tools = _ctx_tools(tmp_path, limit=5)
    first = await tools["finances_context_add"](note="a", author="z")
    assert first["remaining_today"] == 4


async def test_backdated_entry_ages_out_on_next_list(tmp_path: Any) -> None:
    """Silence may mean 'nobody remembers' without the queue growing forever."""
    import time as _t

    from homelab_mcp.finances_context import AGE_OUT_SECONDS, ContextStore

    store = ContextStore(str(tmp_path / "aging.db"))
    old = await store.add(
        author="a",
        note="stale",
        source="volunteered",
        ref_date=None,
        ref_amount=None,
        ref_payee=None,
        created_at=_t.time() - AGE_OUT_SECONDS - 60,
    )
    fresh = await store.add(
        author="a",
        note="recent",
        source="volunteered",
        ref_date=None,
        ref_amount=None,
        ref_payee=None,
    )
    open_rows, aged = await store.list_entries("open", 50)
    assert aged == 1
    assert [r["id"] for r in open_rows] == [fresh["id"]]
    # Retired, never deleted — triage can still find it.
    retired, _ = await store.list_entries("aged_out", 50)
    assert [r["id"] for r in retired] == [old["id"]]


# ── clarify candidates ───────────────────────────────────────────────


def _cand_txns() -> list[dict[str, Any]]:
    return [
        _txn(
            id="big",
            date="2026-07-27",
            amount_cents=-26_614,
            payee_name="Bavarian Inn",
            category_name=None,
        ),
        _txn(
            id="mid",
            date="2026-07-26",
            amount_cents=-13_183,
            payee_name="Off Broadway Shoes",
            category_name=None,
        ),
        # Under the threshold.
        _txn(
            id="small",
            date="2026-07-26",
            amount_cents=-1_200,
            payee_name="Coffee",
            category_name=None,
        ),
        # Already categorized.
        _txn(
            id="done",
            date="2026-07-25",
            amount_cents=-50_000,
            payee_name="Known",
            category_name="Fixed",
        ),
        # Too fresh to have settled.
        _txn(
            id="fresh",
            date="2026-07-31",
            amount_cents=-90_000,
            payee_name="Today",
            category_name=None,
        ),
        # Account-setup artifact.
        _txn(
            id="sb",
            date="2026-07-26",
            amount_cents=-500_000,
            payee_name="Starting Balance",
            category_name=None,
        ),
    ]


async def test_clarify_candidates_selection_is_deterministic(
    tmp_path: Any, httpx_mock: HTTPXMock, frozen_now: None
) -> None:
    httpx_mock.add_response(
        url=re.compile(re.escape(BASE) + r"/transactions.*"),
        json={"transactions": _cand_txns()},
    )
    out = await _ctx_tools(tmp_path)["finances_clarify_candidates"]()
    ids = [c["id"] for c in out["candidates"]]
    # Largest first; small/categorized/fresh/setup all excluded.
    assert ids == ["big", "mid"]


async def test_clarify_candidates_excludes_refs_with_open_context(
    tmp_path: Any, httpx_mock: HTTPXMock, frozen_now: None
) -> None:
    """Re-asking is how a channel trains people to ignore it."""
    tools = _ctx_tools(tmp_path)
    await tools["finances_context_add"](
        note="Birthday dinner.",
        author="a",
        ref_date="2026-07-27",
        ref_amount=266.14,
        ref_payee="Bavarian",
    )
    httpx_mock.add_response(
        url=re.compile(re.escape(BASE) + r"/transactions.*"),
        json={"transactions": _cand_txns()},
    )
    out = await tools["finances_clarify_candidates"]()
    assert [c["id"] for c in out["candidates"]] == ["mid"]
    assert out["excluded_with_open_context"] == 1


async def test_a_ref_with_no_hints_never_suppresses_a_candidate(
    tmp_path: Any, httpx_mock: HTTPXMock, frozen_now: None
) -> None:
    """A pre-posting note is worth storing but must not blank the queue."""
    tools = _ctx_tools(tmp_path)
    await tools["finances_context_add"](note="bought something", author="a")
    httpx_mock.add_response(
        url=re.compile(re.escape(BASE) + r"/transactions.*"),
        json={"transactions": _cand_txns()},
    )
    out = await tools["finances_clarify_candidates"]()
    assert [c["id"] for c in out["candidates"]] == ["big", "mid"]
    assert out["excluded_with_open_context"] == 0


async def test_clarify_candidates_respects_max(
    tmp_path: Any, httpx_mock: HTTPXMock, frozen_now: None
) -> None:
    httpx_mock.add_response(
        url=re.compile(re.escape(BASE) + r"/transactions.*"),
        json={"transactions": _cand_txns()},
    )
    out = await _ctx_tools(tmp_path)["finances_clarify_candidates"](max_items=1)
    assert out["returned"] == 1
    assert out["total"] == 2
    assert out["truncated"] is True


# ── floor exclusions (allocation/obligation, not consumption) ─────────


def _excl_cfg(tmp_path: Any, cats: list[str] | None = None, name: str = "excl.json") -> str:
    cfg = tmp_path / name
    body: dict[str, Any] = {
        "recurring": {"items": []},
        "buffer": {"cash_accounts": [], "card_accounts": []},
    }
    if cats is not None:
        body["floor_excluded_categories"] = cats
    cfg.write_text(json.dumps(body))
    return str(cfg)


def _excl_tools(tmp_path: Any, cats: list[str] | None = None, name: str = "excl.json") -> Any:
    mcp = CapturingMCP()
    register(
        mcp,  # type: ignore[arg-type]
        Settings(
            _env_file=None,  # type: ignore[call-arg]
            oauth_required=False,
            finances_sidecar_base_url=BASE,
            finances_config_path=_excl_cfg(tmp_path, cats, name),
            finances_floor=8400.0,
        ),
    )
    return mcp.tools


def _excl_categories() -> dict[str, Any]:
    return {
        "categories": [
            {"id": "c1", "name": "Fixed", "group_name": "H", "is_income": False},
            {"id": "c2", "name": "Savings/Investments", "group_name": "H", "is_income": False},
            {"id": "c3", "name": "Taxes", "group_name": "H", "is_income": False},
            {"id": "c4", "name": "Income", "group_name": "Income", "is_income": True},
        ]
    }


def _excl_txns() -> list[dict[str, Any]]:
    return [
        _txn(id="a", date="2026-07-05", amount_cents=-100_000, category_name="Fixed"),
        _txn(id="b", date="2026-07-06", amount_cents=-50_000, category_name="Savings/Investments"),
        # The IRS prior-year balance payment.
        _txn(id="t", date="2026-07-07", amount_cents=-1_911_632, category_name="Taxes"),
    ]


def _excl_mock(httpx_mock: HTTPXMock) -> None:
    httpx_mock.add_response(url=f"{BASE}/categories", json=_excl_categories())
    httpx_mock.add_response(
        url=re.compile(re.escape(BASE) + r"/transactions.*"),
        json={"transactions": _excl_txns()},
    )


async def test_taxes_are_excluded_from_the_floor_comparator(
    tmp_path: Any, httpx_mock: HTTPXMock
) -> None:
    """A tax obligation being settled is not a month's discretionary choice.

    Without this a $19,116.32 prior-year balance reads as a ~$19k overspend.
    """
    _excl_mock(httpx_mock)
    out = await _excl_tools(tmp_path)["finances_monthly_summary"](month="2026-07")
    assert out["total_spend"] == pytest.approx(1000.0 + 500.0 + 19_116.32)
    assert out["consumption_spend"] == 1000.0
    assert out["taxes"] == pytest.approx(19_116.32)
    assert out["savings_contributions"] == 500.0
    # Reported, never silently dropped.
    assert out["excluded_from_floor"]["Taxes"] == pytest.approx(19_116.32)
    assert out["excluded_from_floor_total"] == pytest.approx(19_616.32)
    assert out["gap_vs_floor"] == pytest.approx(1000.0 - 8400.0)


async def test_room_excludes_the_same_categories(
    tmp_path: Any, httpx_mock: HTTPXMock, frozen_now: None
) -> None:
    _excl_mock(httpx_mock)
    out = await _excl_tools(tmp_path)["finances_room"]()
    assert out["consumption_mtd"] == 1000.0
    assert out["taxes_mtd"] == pytest.approx(19_116.32)
    assert out["savings_mtd"] == 500.0
    # Pace and room are untouched by the tax payment.
    assert out["room_this_month"] == pytest.approx(8400.0 - 1000.0)


async def test_both_tools_agree_on_the_exclusion_set(
    tmp_path: Any, httpx_mock: HTTPXMock, frozen_now: None
) -> None:
    """One config entry drives both, so they cannot drift apart.

    The two tools share the bucketing via the v0.14.2 refactor; this asserts
    the shared path actually holds rather than being two copies that agree
    today by coincidence.
    """
    tools = _excl_tools(tmp_path)
    _excl_mock(httpx_mock)
    summary = await tools["finances_monthly_summary"](month="2026-07")
    _excl_mock(httpx_mock)
    room = await tools["finances_room"]()

    assert summary["consumption_spend"] == room["consumption_mtd"]
    assert summary["savings_contributions"] == room["savings_mtd"]
    assert summary["taxes"] == room["taxes_mtd"]
    assert set(summary["excluded_from_floor"]) == set(room["excluded_mtd"])
    assert summary["excluded_from_floor"] == room["excluded_mtd"]


async def test_exclusion_list_is_config_driven(tmp_path: Any, httpx_mock: HTTPXMock) -> None:
    """Removing Taxes from config puts it straight back into consumption."""
    _excl_mock(httpx_mock)
    out = await _excl_tools(tmp_path, cats=["Savings/Investments"], name="savings_only.json")[
        "finances_monthly_summary"
    ](month="2026-07")
    assert out["consumption_spend"] == pytest.approx(1000.0 + 19_116.32)
    assert out["excluded_from_floor"] == {"Savings/Investments": 500.0}


def test_shipped_config_excludes_savings_and_taxes() -> None:
    cats = _shipped()["floor_excluded_categories"]
    assert "Savings/Investments" in cats
    assert "Taxes" in cats


async def test_other_categories_are_unaffected(tmp_path: Any, httpx_mock: HTTPXMock) -> None:
    _excl_mock(httpx_mock)
    out = await _excl_tools(tmp_path)["finances_monthly_summary"](month="2026-07")
    by = {r["category"]: r["spend"] for r in out["spend_by_category"]}
    # Still itemized in the breakdown — excluded from the comparator, not hidden.
    assert by["Fixed"] == 1000.0
    assert by["Taxes"] == pytest.approx(19_116.32)
