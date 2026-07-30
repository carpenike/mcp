"""Finance-tool tests.

The arithmetic here is the reason this category exists in code rather than in
a prompt, so these tests pin the rules that are easy to get quietly wrong:
what counts as spend, staleness thresholds and their per-account overrides,
which side of a liability account a payment lands on, and the refusal to
invent a floor that hasn't been decided.

All upstream calls are mocked — the real sidecar is never contacted from CI.
"""

from __future__ import annotations

import re
from collections.abc import Callable
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


# ── staleness ────────────────────────────────────────────────────────


async def test_sync_status_flags_stale_account_against_a_fixed_cutoff(
    tools: dict[str, Callable[..., Any]], httpx_mock: HTTPXMock, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A frozen 'today' makes the threshold arithmetic assertable."""
    import homelab_mcp.tools.finances as fin

    class FrozenDate(fin.date):  # type: ignore[misc,valid-type]
        @classmethod
        def today(cls) -> Any:
            return fin.date(2026, 7, 30)

    monkeypatch.setattr(fin, "date", FrozenDate)

    httpx_mock.add_response(url=f"{BASE}/accounts", json=_accounts())
    httpx_mock.add_response(
        url=re.compile(re.escape(BASE) + r"/transactions.*"),
        json={
            "transactions": [
                # Fresh: today.
                _txn(id="1", account_id="chk", date="2026-07-30"),
                # 21 days stale against the default 3-day threshold.
                _txn(id="2", account_id="heloc", date="2026-07-09"),
                # 30 days stale, but Apple Card's override allows 35.
                _txn(id="3", account_id="apple", date="2026-06-30"),
            ]
        },
    )
    out = await tools["finances_sync_status"]()
    rows = {r["account"]: r for r in out["accounts"]}

    assert rows["USAA Checking"]["days_stale"] == 0
    assert rows["USAA Checking"]["status"] == "fresh"

    assert rows["Spectra HELOC"]["days_stale"] == 21
    assert rows["Spectra HELOC"]["status"] == "dead"  # > 3x threshold

    # The override is the point: a 3-day rule would flag this every month.
    assert rows["Apple Card"]["days_stale"] == 30
    assert rows["Apple Card"]["threshold_days"] == 35
    assert rows["Apple Card"]["threshold_is_override"] is True
    assert rows["Apple Card"]["status"] == "fresh"

    # Off-budget accounts are not sync-monitored.
    assert "House" not in rows
    assert out["overall_status"] == "dead"


# ── recurring ────────────────────────────────────────────────────────


async def test_recurring_matches_payment_on_the_liability_account(
    tools: dict[str, Callable[..., Any]], httpx_mock: HTTPXMock, tmp_path: Any
) -> None:
    """A payment toward a card/loan is a POSITIVE amount on that account."""
    import json

    cfg = tmp_path / "rec.json"
    cfg.write_text(
        json.dumps(
            {
                "items": [
                    {
                        "name": "Synchrony",
                        "amount": 291.0,
                        "tolerance": 5.0,
                        "match_any": ["synchrony"],
                        "accounts": ["Synchrony Container Store"],
                        "ends": None,
                    }
                ]
            }
        )
    )
    monkey = Settings(
        _env_file=None,  # type: ignore[call-arg]
        oauth_required=False,
        finances_sidecar_base_url=BASE,
        finances_recurring_config_path=str(cfg),
    )
    mcp = CapturingMCP()
    register(mcp, monkey)  # type: ignore[arg-type]

    httpx_mock.add_response(
        url=re.compile(re.escape(BASE) + r"/transactions.*"),
        json={
            "transactions": [
                _txn(
                    id="p",
                    date="2026-07-15",
                    amount_cents=29_200,  # positive: paying the balance down
                    account_id="syn",
                    account_name="Synchrony Container Store",
                    payee_name="Payment",
                )
            ]
        },
    )
    out = await mcp.tools["finances_recurring"](month="2026-07")
    row = out["obligations"][0]
    assert row["status"] == "MATCHED"
    assert row["actual_amount"] == 292.0


async def test_recurring_reports_ended_not_missing_past_end_month(
    httpx_mock: HTTPXMock, tmp_path: Any
) -> None:
    import json

    cfg = tmp_path / "rec.json"
    cfg.write_text(
        json.dumps(
            {
                "items": [
                    {
                        "name": "Saxophone",
                        "amount": 97.0,
                        "tolerance": 5.0,
                        "match_any": ["music"],
                        "ends": "2026-11",
                    }
                ]
            }
        )
    )
    mcp = CapturingMCP()
    register(
        mcp,  # type: ignore[arg-type]
        Settings(
            _env_file=None,  # type: ignore[call-arg]
            oauth_required=False,
            finances_sidecar_base_url=BASE,
            finances_recurring_config_path=str(cfg),
        ),
    )
    httpx_mock.add_response(
        url=re.compile(re.escape(BASE) + r"/transactions.*"), json={"transactions": []}
    )
    out = await mcp.tools["finances_recurring"](month="2026-12")
    # A finished loan must stop generating MISSING alarms forever.
    assert out["obligations"][0]["status"] == "ENDED"
    assert out["needs_attention"] == []


# ── debt ─────────────────────────────────────────────────────────────


async def test_debt_status_equity_is_fully_derived_from_actual(
    tools: dict[str, Callable[..., Any]], httpx_mock: HTTPXMock
) -> None:
    """No config input: house, mortgage and HELOC all come from the budget."""
    httpx_mock.add_response(url=f"{BASE}/accounts", json=_accounts())
    httpx_mock.add_response(
        url=re.compile(re.escape(BASE) + r"/transactions.*"), json={"transactions": []}
    )
    out = await tools["finances_debt_status"]()
    # 850,000.00 - 392,172.46 - 109,290.04
    assert out["home_equity"] == pytest.approx(348_537.50)
    assert out["mortgage"]["balance"] == -392_172.46
    assert out["heloc"]["balance"] == -109_290.04
    assert out["house_value"] == 850_000.0
    assert out["home_equity_note"] is None


async def test_debt_status_total_debt_includes_offbudget_mortgage(
    tools: dict[str, Callable[..., Any]], httpx_mock: HTTPXMock
) -> None:
    httpx_mock.add_response(url=f"{BASE}/accounts", json=_accounts())
    httpx_mock.add_response(
        url=re.compile(re.escape(BASE) + r"/transactions.*"), json={"transactions": []}
    )
    out = await tools["finances_debt_status"]()
    # mortgage + HELOC + Apple Card; the House and Checking are assets.
    assert out["total_debt"] == pytest.approx(-(392_172.46 + 109_290.04 + 93.08))


async def test_debt_status_never_flags_the_mortgage_as_revolving(
    tools: dict[str, Callable[..., Any]], httpx_mock: HTTPXMock
) -> None:
    """The card-creep heuristic must not fire on instalment/credit-line debt."""
    httpx_mock.add_response(url=f"{BASE}/accounts", json=_accounts())
    httpx_mock.add_response(
        url=re.compile(re.escape(BASE) + r"/transactions.*"), json={"transactions": []}
    )
    out = await tools["finances_debt_status"]()
    assert "Mortgage (NewRez)" not in out["cards_flagged"]
    assert "Spectra HELOC" not in out["cards_flagged"]
    # Off-budget accounts stay out of the on-budget debts list entirely.
    assert all(r["account"] != "Mortgage (NewRez)" for r in out["debts"])


async def test_debt_status_equity_null_when_mortgage_account_missing(
    httpx_mock: HTTPXMock,
) -> None:
    """A renamed/unlinked account must fail loudly, not silently understate."""
    accounts = _accounts()
    accounts["accounts"] = [a for a in accounts["accounts"] if a["id"] != "mortgage"]
    mcp = CapturingMCP()
    register(
        mcp,  # type: ignore[arg-type]
        Settings(
            _env_file=None,  # type: ignore[call-arg]
            oauth_required=False,
            finances_sidecar_base_url=BASE,
        ),
    )
    httpx_mock.add_response(url=f"{BASE}/accounts", json=accounts)
    httpx_mock.add_response(
        url=re.compile(re.escape(BASE) + r"/transactions.*"), json={"transactions": []}
    )
    out = await mcp.tools["finances_debt_status"]()
    assert out["home_equity"] is None
    assert "Mortgage" in out["home_equity_note"]
    assert out["mortgage"] is None


# ── error contract ───────────────────────────────────────────────────


async def test_sidecar_down_returns_structured_error_not_raise(
    tools: dict[str, Callable[..., Any]], httpx_mock: HTTPXMock
) -> None:
    import httpx

    httpx_mock.add_exception(httpx.ConnectError("refused"))
    out = await tools["finances_debt_status"]()
    assert out["error"]["code"] == "actual_unreachable"
