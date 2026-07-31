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
    _mock_month(httpx_mock, [_txn(amount_cents=-1_000_000)])
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
                "amount": 44.0,
                "match_any": ["usaa life insurance", "usaa.com pay int life"],
                "ends": None,
            }
        ],
    )
    out = await tools["finances_recurring"](month="2026-06")
    row = out["obligations"][0]
    assert row["status"] == "MATCHED"
    assert row["actual_amount"] == 44.15
    # 0.15 on a $44 bill is noise, not a price rise.
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
