"""Household finance tools backed by Actual Budget.

These tools are deterministic: they read the budget and compute numbers.
They never compose prose, never give advice, and never decide anything —
that's the caller's job (an advisor session, or hermes-agent composing the
weekly pulse). Every figure here should be reproducible by hand from the
same budget, which is the whole point of moving the gap math out of a
model and into code.

Data path: Actual exposes no HTTP query API and no API keys, so all reads
go through the loopback Node sidecar in `sidecar/` (see its header for the
version-pinning and single-login constraints it exists to enforce). This
module holds the arithmetic; the sidecar holds the Actual client.

Money: Actual stores integer cents. Cents stay on the wire and are
converted to dollars exactly once, at the tool boundary, via `_d()`.
"""

from __future__ import annotations

import calendar
import json
import logging
import pathlib
from collections import defaultdict
from datetime import date, timedelta
from typing import TYPE_CHECKING, Annotated, Any

from mcp.types import ToolAnnotations
from pydantic import Field

from homelab_mcp.tools._http import ToolError, make_client, request_json

if TYPE_CHECKING:
    import httpx
    from mcp.server.fastmcp import FastMCP

    from homelab_mcp.config import Settings

log = logging.getLogger(__name__)

_RO = ToolAnnotations(
    readOnlyHint=True, destructiveHint=False, idempotentHint=True, openWorldHint=False
)
# trigger_sync=True asks Actual to pull from the banks, so this one isn't
# idempotent. openWorld stays False: we call the fixed internal sidecar, and
# the outbound bank fetch happens on the Actual server, not from here.
_RO_SYNC = ToolAnnotations(
    readOnlyHint=True, destructiveHint=False, idempotentHint=False, openWorldHint=False
)

# The housekeeping category. Credit-card payments and inter-account moves are
# real money movement but NOT household spend — counting them double-counts
# every card purchase (once when charged, once when the card is paid). Excluded
# from every spend figure this module produces.
EXCLUDED_CATEGORY = "CC Payments & Transfers"

_DEFAULT_RECURRING = pathlib.Path(__file__).with_name("finances_recurring.json")

INSTRUCTIONS = """\
finances_* tools read a self-hosted Actual Budget and return computed numbers,
never narrative. Notes that apply across all of them:

- Always lead with `finances_sync_status`. Every other figure is only as good
  as the last bank sync, and a silent sync outage has happened before (Dec
  2025 - Apr 2026). If accounts are stale, say so before quoting any total.
- Amounts are returned in dollars, already signed for the reader: `spend` is
  positive money out, `income` positive money in.
- Spend figures always exclude the "CC Payments & Transfers" category and
  off-budget accounts. Card payments are not spend.
- `uncategorized` in finances_monthly_summary is a data-quality signal, not a
  category. A large value means the category totals understate reality —
  report it rather than quietly summing around it.
- `gap_vs_floor` is null until a floor is configured. Null means "not decided
  yet"; do not substitute a guess.
"""


def _d(cents: int) -> float:
    """Convert Actual's integer cents to dollars, rounded to the cent."""
    return round(cents / 100.0, 2)


def _month_bounds(month: str) -> tuple[date, date]:
    """Return (first, last) dates of a 'YYYY-MM' month."""
    try:
        year, mon = (int(p) for p in month.split("-", 1))
        first = date(year, mon, 1)
    except (ValueError, TypeError) as exc:
        raise ToolError(
            "finances_bad_month",
            f"Invalid month {month!r}.",
            "Use 'YYYY-MM', e.g. '2026-07'.",
        ) from exc
    return first, date(year, mon, calendar.monthrange(year, mon)[1])


def _shift_month(anchor: date, back: int) -> date:
    """First day of the month `back` months before `anchor`'s month."""
    total = anchor.year * 12 + (anchor.month - 1) - back
    return date(total // 12, total % 12 + 1, 1)


def register(mcp: FastMCP, settings: Settings) -> None:
    """Register the finances_* tools on the given MCP server."""
    base = settings.finances_sidecar_base_url.rstrip("/")
    headers = (
        {"X-Sidecar-Token": settings.finances_sidecar_token}
        if settings.finances_sidecar_token
        else {}
    )
    # Generous timeout: the sidecar may re-sync with the Actual server mid-call.
    client: httpx.AsyncClient = make_client(headers=headers, timeout=60.0)

    def _config() -> dict[str, Any]:
        """Load the recurring-obligations config (operator file, else packaged)."""
        path = pathlib.Path(settings.finances_recurring_config_path or _DEFAULT_RECURRING)
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise ToolError(
                "finances_config_unreadable",
                f"Could not read the recurring-obligations config at {path}.",
                "Check HOMELAB_MCP_FINANCES_RECURRING_CONFIG_PATH and that the file is valid JSON.",
            ) from exc
        if not isinstance(data, dict):
            raise ToolError(
                "finances_config_invalid",
                "The recurring-obligations config must be a JSON object.",
                "",
            )
        return data

    async def _get(path: str, params: dict[str, Any] | None = None) -> Any:
        if not base:
            raise ToolError(
                "finances_not_configured",
                "The Actual sidecar is not configured.",
                "Set HOMELAB_MCP_FINANCES_SIDECAR_BASE_URL (and its token).",
            )
        return await request_json(
            client,
            "GET",
            f"{base}{path}",
            service="actual",
            params=params,
            unreachable_hint=(
                "Check that the homelab-mcp-actual-sidecar service is running "
                "and that HOMELAB_MCP_FINANCES_SIDECAR_BASE_URL points at it."
            ),
        )

    async def _accounts() -> list[dict[str, Any]]:
        return list((await _get("/accounts"))["accounts"])

    async def _categories() -> list[dict[str, Any]]:
        return list((await _get("/categories"))["categories"])

    async def _transactions(start: date, end: date) -> list[dict[str, Any]]:
        data = await _get("/transactions", {"start": start.isoformat(), "end": end.isoformat()})
        return list(data["transactions"])

    def _income_category_names(categories: list[dict[str, Any]]) -> set[str]:
        """Names of categories in an income group (Income, Starting Balances)."""
        return {c["name"] for c in categories if c.get("is_income")}

    def _is_spend(txn: dict[str, Any], income_names: set[str]) -> bool:
        """True if a transaction counts as household spend.

        Excludes off-budget accounts (investments, the house), transfer legs,
        the CC-payments housekeeping category, income, and inflows.
        """
        return (
            not txn["account_offbudget"]
            and not txn["is_transfer"]
            and txn["category_name"] != EXCLUDED_CATEGORY
            and txn["category_name"] not in income_names
            and txn["amount_cents"] < 0
        )

    # ── 1. sync status ───────────────────────────────────────────────
    @mcp.tool(
        annotations=_RO_SYNC,
        name="finances_sync_status",
        description=(
            "Check how fresh the bank data behind every other finances tool is. "
            "Returns, per on-budget account, the date of its most recent posted "
            "transaction, how many days stale that is, and whether it breaches "
            "its staleness threshold; plus an overall fresh/stale/dead verdict. "
            "CALL THIS FIRST in any financial review or weekly pulse — a silent "
            "sync outage makes every other number confidently wrong, and one has "
            "happened before. Set trigger_sync=true to ask Actual to pull from "
            "the banks before reporting (slower; use for an on-demand refresh, "
            "not on every call)."
        ),
    )
    async def sync_status(
        trigger_sync: Annotated[
            bool,
            Field(description="Run a bank sync before reporting. Slower."),
        ] = False,
    ) -> dict[str, Any]:
        try:
            if trigger_sync:
                if not base:
                    raise ToolError(
                        "finances_not_configured",
                        "The Actual sidecar is not configured.",
                        "Set HOMELAB_MCP_FINANCES_SIDECAR_BASE_URL (and its token).",
                    )
                await request_json(
                    client,
                    "POST",
                    f"{base}/bank-sync",
                    service="actual",
                    unreachable_hint="Check that the Actual sidecar is running.",
                )

            cfg = _config()
            overrides: dict[str, int] = dict(cfg.get("account_stale_overrides") or {})
            accounts = await _accounts()
            today = date.today()
            # 120 days covers even the laziest monthly-statement feed, so an
            # account with no recent activity is reported as an old date rather
            # than indistinguishable from "never synced".
            txns = await _transactions(today - timedelta(days=120), today)
        except ToolError as err:
            return err.payload()

        latest: dict[str, str] = {}
        for t in txns:
            aid = t["account_id"]
            if aid and (aid not in latest or t["date"] > latest[aid]):
                latest[aid] = t["date"]

        rows: list[dict[str, Any]] = []
        for a in accounts:
            if a["offbudget"] or a["closed"]:
                continue
            threshold = int(overrides.get(a["name"], settings.finances_stale_days))
            last = latest.get(a["id"])
            days = (today - date.fromisoformat(last)).days if last else None
            # No transactions in the whole lookback window, or far past the
            # threshold, reads as a broken feed rather than a quiet month.
            if days is None or days > threshold * 3:
                status = "dead"
            elif days > threshold:
                status = "stale"
            else:
                status = "fresh"
            rows.append(
                {
                    "account": a["name"],
                    "latest_transaction_date": last,
                    "days_stale": days,
                    "threshold_days": threshold,
                    "status": status,
                    # Surfaced so a reader can see WHY Apple Card is judged
                    # differently instead of assuming the tool is inconsistent.
                    "threshold_is_override": a["name"] in overrides,
                }
            )

        rows.sort(key=lambda r: (-(r["days_stale"] or 10_000), r["account"]))
        overall = (
            "dead"
            if any(r["status"] == "dead" for r in rows)
            else "stale"
            if any(r["status"] == "stale" for r in rows)
            else "fresh"
        )
        return {
            "as_of": today.isoformat(),
            "overall_status": overall,
            "stale_accounts": [r["account"] for r in rows if r["status"] != "fresh"],
            "accounts": rows,
            "sync_triggered": trigger_sync,
        }

    # ── 2. monthly summary ───────────────────────────────────────────
    @mcp.tool(
        annotations=_RO,
        name="finances_monthly_summary",
        description=(
            "Compute one month's income, spend by category, total spend, and the "
            "gap against the configured monthly floor. Excludes the 'CC Payments "
            "& Transfers' category, transfer legs, and off-budget accounts, so "
            "the total is household spend rather than money movement. For a "
            "month still in progress it also returns a pro-rated pace projection. "
            "Use for 'how are we doing this month', the weekly pulse's gap line, "
            "and monthly reviews. Check the `uncategorized` figure before quoting "
            "category totals — a large value means they understate reality."
        ),
    )
    async def monthly_summary(
        month: Annotated[
            str | None,
            Field(description="Month as 'YYYY-MM'. Defaults to the current month."),
        ] = None,
    ) -> dict[str, Any]:
        try:
            target = month or date.today().strftime("%Y-%m")
            first, last = _month_bounds(target)
            categories = await _categories()
            income_names = _income_category_names(categories)
            txns = await _transactions(first, last)
        except ToolError as err:
            return err.payload()

        by_category: dict[str, int] = defaultdict(int)
        income_cents = 0
        uncategorized_cents = 0
        for t in txns:
            if t["account_offbudget"] or t["is_transfer"]:
                continue
            name = t["category_name"]
            if name in income_names:
                income_cents += t["amount_cents"]
                continue
            if _is_spend(t, income_names):
                key = name or "(uncategorized)"
                by_category[key] += -t["amount_cents"]
                if not name:
                    uncategorized_cents += -t["amount_cents"]

        total_cents = sum(by_category.values())
        today = date.today()
        in_progress = first <= today <= last
        days_elapsed = (today - first).days + 1 if in_progress else (last - first).days + 1
        days_in_month = (last - first).days + 1

        floor = settings.finances_floor
        result: dict[str, Any] = {
            "month": target,
            "month_in_progress": in_progress,
            "days_elapsed": days_elapsed,
            "days_in_month": days_in_month,
            "income": _d(income_cents),
            "total_spend": _d(total_cents),
            "uncategorized": _d(uncategorized_cents),
            "spend_by_category": [
                {"category": k, "spend": _d(v)}
                for k, v in sorted(by_category.items(), key=lambda kv: -kv[1])
            ],
            "excluded_category": EXCLUDED_CATEGORY,
            "floor": floor,
        }
        if floor is None:
            result["gap_vs_floor"] = None
            result["gap_note"] = (
                "No floor configured (HOMELAB_MCP_FINANCES_FLOOR); gap not computed."
            )
        else:
            result["gap_vs_floor"] = round(_d(total_cents) - floor, 2)
            if in_progress:
                # Straight-line pace. Deliberately naive — a weighted model would
                # imply a forecasting confidence this data doesn't support.
                prorated = round(floor * days_elapsed / days_in_month, 2)
                projected = round(_d(total_cents) * days_in_month / days_elapsed, 2)
                result["prorated_floor_to_date"] = prorated
                result["gap_vs_prorated_floor"] = round(_d(total_cents) - prorated, 2)
                result["projected_month_end_spend"] = projected
        return result

    # ── 3. recurring obligations ─────────────────────────────────────
    @mcp.tool(
        annotations=_RO,
        name="finances_recurring",
        description=(
            "Check this month's expected fixed obligations (mortgage, car loan, "
            "HELOC, utilities, insurance, financed purchases) against what has "
            "actually posted. Returns one row per obligation marked MATCHED, "
            "CHANGED (posted, but outside the tolerance band), MISSING (nothing "
            "posted yet), or ENDED (past its final month). Use for late-fee "
            "prevention in the weekly pulse and to catch silent price rises. "
            "The expected list is operator-maintained config, not inferred from "
            "history, so a genuinely new bill shows up as unmatched spend "
            "elsewhere rather than being silently absorbed here."
        ),
    )
    async def recurring(
        month: Annotated[
            str | None,
            Field(description="Month as 'YYYY-MM'. Defaults to the current month."),
        ] = None,
    ) -> dict[str, Any]:
        try:
            target = month or date.today().strftime("%Y-%m")
            first, last = _month_bounds(target)
            cfg = _config()
            txns = await _transactions(first, last)
        except ToolError as err:
            return err.payload()

        items = cfg.get("items") or []
        # Sign depends on which side of the obligation we observe. A bill paid
        # from checking is a NEGATIVE amount there. The same obligation seen on
        # the liability account it services (Synchrony, a card) is a POSITIVE
        # amount — paying down a balance is an inflow to that account. Both are
        # kept here; each item picks the side it cares about below.
        candidates = [t for t in txns if not t["account_offbudget"] and t["amount_cents"] != 0]
        claimed: set[str] = set()
        rows: list[dict[str, Any]] = []

        for item in items:
            name = str(item.get("name", "?"))
            ends = item.get("ends")
            if ends and target > str(ends):
                rows.append({"name": name, "status": "ENDED", "ended_after": ends})
                continue

            expected = float(item.get("amount", 0.0))
            tol = float(item.get("tolerance", 0.0))
            needles = [str(s).lower() for s in (item.get("match_any") or [])]
            acct_filter = [str(s).lower() for s in (item.get("accounts") or [])]

            best: dict[str, Any] | None = None
            for t in candidates:
                if t["id"] in claimed:
                    continue
                # A dedicated account (Synchrony Container Store, Apple Card)
                # narrows the search; it never widens it.
                if acct_filter:
                    if not any(a in (t.get("account_name") or "").lower() for a in acct_filter):
                        continue
                    # On the obligation's own account the servicing payment is
                    # the positive leg, so compare on magnitude.
                    amt = _d(abs(t["amount_cents"]))
                else:
                    # Elsewhere, only money leaving an account can pay a bill.
                    if t["amount_cents"] >= 0:
                        continue
                    amt = _d(-t["amount_cents"])
                hay = f"{t.get('payee_name') or ''} {t.get('notes') or ''}".lower()
                # Identify by payee, OR — on an account dedicated to this one
                # obligation — by the amount landing in the tolerance band.
                # SimpleFin payee strings for these financed accounts are
                # inconsistent ("SYNCHRONY BANK" vs the merchant name), so
                # requiring a payee hit reported every such payment as MISSING.
                if not any(n in hay for n in needles) and not (
                    acct_filter and abs(amt - expected) <= tol
                ):
                    continue
                # Prefer the candidate closest to the expected amount, so a
                # $9 Apple Store charge can't claim the $200.33 installment.
                if best is None or abs(amt - expected) < abs(best["_amt"] - expected):
                    best = {**t, "_amt": amt}

            if best is None:
                rows.append(
                    {
                        "name": name,
                        "status": "MISSING",
                        "expected_amount": expected,
                        "expected_day": item.get("expected_day"),
                    }
                )
                continue

            claimed.add(best["id"])
            actual_amt = best["_amt"]
            within = abs(actual_amt - expected) <= tol
            posted = date.fromisoformat(best["date"])
            rows.append(
                {
                    "name": name,
                    "status": "MATCHED" if within else "CHANGED",
                    "expected_amount": expected,
                    "actual_amount": actual_amt,
                    "delta": round(actual_amt - expected, 2),
                    "tolerance": tol,
                    "expected_day": item.get("expected_day"),
                    "posted_date": best["date"],
                    "posted_day": posted.day,
                    "account": best.get("account_name"),
                    "payee": best.get("payee_name"),
                }
            )

        order = {"MISSING": 0, "CHANGED": 1, "MATCHED": 2, "ENDED": 3}
        rows.sort(key=lambda r: (order.get(str(r["status"]), 9), str(r["name"])))
        counts: dict[str, int] = defaultdict(int)
        for r in rows:
            counts[str(r["status"])] += 1
        return {
            "month": target,
            "month_in_progress": first <= date.today() <= last,
            "counts": dict(counts),
            "needs_attention": [r["name"] for r in rows if r["status"] in ("MISSING", "CHANGED")],
            "obligations": rows,
        }

    # ── 4. trend ─────────────────────────────────────────────────────
    @mcp.tool(
        annotations=_RO,
        name="finances_trend",
        description=(
            "Per-category monthly spend series plus an income series for the "
            "last N complete-or-partial months, on the same exclusion rules as "
            "finances_monthly_summary. Use for monthly/quarterly reviews, "
            "spotting a category drifting upward, and charting. Returns months "
            "oldest-first. Note the most recent month may be partial — check "
            "`months[-1].in_progress` before comparing it to the others."
        ),
    )
    async def trend(
        months: Annotated[int, Field(ge=1, le=24, description="How many months back.")] = 6,
    ) -> dict[str, Any]:
        try:
            today = date.today()
            start = _shift_month(today, months - 1)
            _, end = _month_bounds(today.strftime("%Y-%m"))
            categories = await _categories()
            income_names = _income_category_names(categories)
            txns = await _transactions(start, end)
        except ToolError as err:
            return err.payload()

        per_month: dict[str, dict[str, int]] = defaultdict(lambda: defaultdict(int))
        income_by_month: dict[str, int] = defaultdict(int)
        for t in txns:
            if t["account_offbudget"] or t["is_transfer"]:
                continue
            key = t["date"][:7]
            if t["category_name"] in income_names:
                income_by_month[key] += t["amount_cents"]
                continue
            if _is_spend(t, income_names):
                per_month[key][t["category_name"] or "(uncategorized)"] += -t["amount_cents"]

        labels = [_shift_month(today, i).strftime("%Y-%m") for i in range(months - 1, -1, -1)]
        current = today.strftime("%Y-%m")
        seen = sorted({c for m in per_month.values() for c in m})
        return {
            "months": [
                {
                    "month": label,
                    "in_progress": label == current,
                    "income": _d(income_by_month.get(label, 0)),
                    "total_spend": _d(sum(per_month.get(label, {}).values())),
                    "by_category": {c: _d(per_month.get(label, {}).get(c, 0)) for c in seen},
                }
                for label in labels
            ],
            "categories": seen,
            "excluded_category": EXCLUDED_CATEGORY,
        }

    # ── 5. debt status ───────────────────────────────────────────────
    @mcp.tool(
        annotations=_RO,
        name="finances_debt_status",
        description=(
            "The debt scoreboard: current balances of the mortgage, HELOC, the "
            "Synchrony financed purchases and every credit card, plus total "
            "debt and the HELOC's change over the last 7 and 30 days (the "
            "paydown progress number). Flags any card carrying more than $500 "
            "late in its cycle, which is the early signal that revolving "
            "balances are creeping back after the July 2026 payoff. Also "
            "reports home equity — house value minus mortgage minus HELOC, all "
            "read from Actual. Use for the pulse's debt line and any "
            "deleveraging discussion. Note equity is display-only: it is not an "
            "affordability input."
        ),
    )
    async def debt_status() -> dict[str, Any]:
        try:
            today = date.today()
            accounts = await _accounts()
            txns = await _transactions(today - timedelta(days=31), today)
        except ToolError as err:
            return err.payload()

        # Reconstruct historical balances by unwinding recent activity from the
        # current balance. Actual has no as-of-date balance API, and deriving it
        # this way stays correct regardless of when the account last synced.
        def balance_as_of(account_id: str, current_cents: int, cutoff: date) -> int:
            delta = sum(
                int(t["amount_cents"])
                for t in txns
                if t["account_id"] == account_id and date.fromisoformat(t["date"]) > cutoff
            )
            return current_cents - delta

        open_accounts = [a for a in accounts if not a["closed"]]
        by_name = {a["name"]: a for a in open_accounts}

        def _find(needle: str) -> dict[str, Any] | None:
            """The one open account whose name contains `needle`, else None."""
            hits = [a for a in open_accounts if needle in a["name"].lower()]
            return hits[0] if len(hits) == 1 else None

        heloc = _find("heloc")
        # The mortgage is off-budget (it services the house, it isn't monthly
        # cashflow), so it is looked up by name rather than swept up by the
        # on-budget loop below.
        mortgage = _find("mortgage")

        debts: list[dict[str, Any]] = []
        cards_flagged: list[str] = []

        for a in accounts:
            if a["closed"] or a["offbudget"]:
                continue
            cents = a["balance_cents"]
            if cents >= 0:
                continue  # asset-side account, not debt
            row: dict[str, Any] = {"account": a["name"], "balance": _d(cents)}
            # A card is "creeping back" if it carries a real balance this late
            # in the month — before ~day 25 an unpaid statement is just normal
            # cycle activity, not revolving debt. Instalment/credit-line
            # accounts carry a balance by design and must never be flagged.
            name = a["name"].lower()
            is_card = "synchrony" not in name and "heloc" not in name and "mortgage" not in name
            if is_card and -cents > 50_000 and today.day > 25:
                row["flag"] = "balance over $500 late in the cycle — revolving may be returning"
                cards_flagged.append(a["name"])
            debts.append(row)
        heloc_block: dict[str, Any] | None = None
        if heloc:
            cur = heloc["balance_cents"]
            b7 = balance_as_of(heloc["id"], cur, today - timedelta(days=7))
            b30 = balance_as_of(heloc["id"], cur, today - timedelta(days=30))
            heloc_block = {
                "account": heloc["name"],
                "balance": _d(cur),
                "balance_7d_ago": _d(b7),
                "balance_30d_ago": _d(b30),
                # Negative change = balance moved toward zero = paydown.
                "change_7d": _d(cur - b7),
                "change_30d": _d(cur - b30),
            }

        # Home equity is entirely derived now that the mortgage is a synced
        # account: every input except the house valuation comes from Actual.
        # If an account can't be identified we report null and say which one —
        # a silently stale hand-maintained number was the failure mode this
        # replaced, and guessing would reintroduce it.
        equity: float | None = None
        equity_note = None
        house = by_name.get("House")
        missing = [
            label
            for label, account in (("House", house), ("Mortgage", mortgage))
            if account is None
        ]
        if missing:
            equity_note = (
                f"Could not identify a unique {' and '.join(missing)} account in Actual; "
                "equity not computed. Check the account name, or that only one open "
                "account matches."
            )
        else:
            assert house is not None and mortgage is not None
            # Both mortgage and HELOC balances are negative, so adding them
            # subtracts the debt.
            heloc_cents = heloc["balance_cents"] if heloc else 0
            equity = round(
                _d(house["balance_cents"]) + _d(mortgage["balance_cents"]) + _d(heloc_cents),
                2,
            )

        # Every liability in the budget, on- and off-budget alike. The mortgage
        # only became visible here once it was linked as a synced account.
        total_debt_cents = sum(
            int(a["balance_cents"]) for a in open_accounts if a["balance_cents"] < 0
        )

        return {
            "as_of": today.isoformat(),
            "debts": sorted(debts, key=lambda r: r["balance"]),
            "heloc": heloc_block,
            "mortgage": (
                {"account": mortgage["name"], "balance": _d(mortgage["balance_cents"])}
                if mortgage
                else None
            ),
            "cards_flagged": cards_flagged,
            "total_debt": _d(total_debt_cents),
            "home_equity": equity,
            "home_equity_note": equity_note,
            # The only hand-maintained figure left in the system: updated
            # quarterly at review. Display-only per PLAN.md — equity never
            # feeds an affordability or spending decision.
            "house_value": _d(house["balance_cents"]) if house else None,
        }
