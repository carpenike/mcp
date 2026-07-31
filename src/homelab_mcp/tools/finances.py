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
from datetime import UTC, date, datetime, timedelta
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

_DEFAULT_CONFIG = pathlib.Path(__file__).with_name("finances_config.json")

# Feed cadences a `sync.accounts` entry may declare. "manual" accounts (the
# Tesla loan, the house valuation) are maintained by hand and never sync, so
# measuring their staleness is meaningless — they are excluded from the
# verdict rather than permanently red.
CADENCES = ("daily", "monthly", "manual")

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


def _classify(rate: float | None, hurdle: float) -> str:
    """Bucket a debt by cost: worth accelerating, or cheap enough to ride.

    An unconfigured rate stays "unknown" rather than defaulting either way.
    Defaulting to "ride" would tell the household a 20%-APR carried card
    balance is cheap money; defaulting to "accelerate" would cry wolf on every
    grace-period card. Neither is a safe guess, so the tool asks instead.
    """
    if rate is None:
        return "unknown"
    return "accelerate" if rate > hurdle else "ride"


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
        """Load the operator config (operator file, else the packaged copy)."""
        path = pathlib.Path(settings.finances_config_path or _DEFAULT_CONFIG)
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise ToolError(
                "finances_config_unreadable",
                f"Could not read the finances config at {path}.",
                "Check HOMELAB_MCP_FINANCES_CONFIG_PATH and that the file is valid JSON.",
            ) from exc
        if not isinstance(data, dict):
            raise ToolError(
                "finances_config_invalid",
                "The finances config must be a JSON object.",
                "",
            )
        return data

    def _load_state() -> dict[str, Any] | None:
        """Read the classification memo, or None if unavailable/absent."""
        if not settings.finances_state_path:
            return None
        try:
            return dict(
                json.loads(pathlib.Path(settings.finances_state_path).read_text(encoding="utf-8"))
            )
        except FileNotFoundError:
            return {}  # first run: readable location, nothing recorded yet
        except (OSError, ValueError):
            log.warning("finances state file unreadable — class-change detection disabled")
            return None

    def _save_state(state: dict[str, Any]) -> bool:
        """Persist the classification memo. False if it couldn't be written."""
        if not settings.finances_state_path:
            return False
        path = pathlib.Path(settings.finances_state_path)
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps(state, indent=2, sort_keys=True), encoding="utf-8")
        except OSError:
            log.warning("could not write finances state to %s", path)
            return False
        return True

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
            "Check whether the bank data behind every other finances tool is "
            "actually being fed. Each account is judged against ITS OWN expected "
            "cadence — daily feeds go stale in days, monthly statement exports "
            "(Apple Card, Synchrony, HELOC, mortgage) are healthy for weeks, and "
            "hand-maintained accounts are excluded entirely. The overall verdict "
            "is the worst account relative to its own cadence, so a correctly "
            "synced monthly feed never drags everything to 'dead'. Returns per "
            "account: latest posted transaction, its age, the cadence applied, "
            "and fresh/stale/dead. CALL THIS FIRST in any review or weekly pulse "
            "— a silent sync outage makes every other number confidently wrong, "
            "and one went unnoticed for seven months. Set trigger_sync=true to "
            "pull from the banks first (slow; on-demand refresh only)."
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
            sync_cfg = cfg.get("sync") or {}
            cadence_of: dict[str, str] = dict(sync_cfg.get("accounts") or {})
            max_age: dict[str, int] = dict(sync_cfg.get("cadence_max_age_days") or {})
            default_cadence = str(sync_cfg.get("default_cadence") or "daily")
            feed_stale_h = float(sync_cfg.get("feed_stale_hours", 26))
            feed_dead_h = float(sync_cfg.get("feed_dead_hours", 72))
            accounts = await _accounts()
            now = datetime.now(UTC)
            today = date.today()
            # 120 days covers even the laziest monthly feed, so an account with
            # no recent activity reports an old date rather than being
            # indistinguishable from "never synced".
            txns = await _transactions(today - timedelta(days=120), today)
        except ToolError as err:
            return err.payload()

        latest: dict[str, str] = {}
        for t in txns:
            aid = t["account_id"]
            if aid and (aid not in latest or t["date"] > latest[aid]):
                latest[aid] = t["date"]

        rows: list[dict[str, Any]] = []
        manual: list[dict[str, Any]] = []
        for a in accounts:
            if a["closed"]:
                continue
            cadence = cadence_of.get(a["name"])
            if cadence is None:
                # Off-budget accounts are investments/valuations unless the
                # operator explicitly declared a cadence for them; on-budget
                # accounts default to the daily rule so a NEW account is
                # monitored from day one rather than silently unwatched.
                if a["offbudget"]:
                    continue
                cadence = default_cadence
            if cadence not in CADENCES:
                cadence = default_cadence

            last = latest.get(a["id"])
            days = (today - date.fromisoformat(last)).days if last else None
            threshold = int(max_age.get(cadence, 3))

            # ACTIVITY: has anything posted lately? Informational only. A
            # dormant card is not a broken feed, and conflating the two is
            # what made the verdict permanently red.
            if days is None:
                activity = "none"
            elif days > threshold:
                activity = "quiet"
            else:
                activity = "active"

            # FEED HEALTH: when did the bank feed last fetch? This is the
            # signal the verdict is built on.
            raw_sync = a.get("last_sync")
            feed_age_h: float | None = None
            if raw_sync:
                try:
                    fetched = datetime.fromtimestamp(int(raw_sync) / 1000.0, tz=UTC)
                    feed_age_h = round((now - fetched).total_seconds() / 3600.0, 1)
                except (TypeError, ValueError, OSError, OverflowError):
                    feed_age_h = None

            row: dict[str, Any] = {
                "account": a["name"],
                "cadence": cadence,
                "latest_transaction_date": last,
                "days_since_last_transaction": days,
                "activity": activity,
                "activity_threshold_days": threshold,
                "feed_age_hours": feed_age_h,
            }

            if cadence == "manual":
                # Maintained by hand: it has no feed to be healthy or broken.
                row["status"] = "manual"
                row["basis"] = "manual"
                manual.append(row)
                continue

            if feed_age_h is not None:
                row["basis"] = "feed"
                if feed_age_h > feed_dead_h:
                    row["status"] = "dead"
                elif feed_age_h > feed_stale_h:
                    row["status"] = "stale"
                else:
                    row["status"] = "fresh"
            else:
                # No feed timestamp and not declared manual: fall back to
                # activity, and say so, rather than passing it silently.
                row["basis"] = "activity_fallback"
                row["basis_note"] = (
                    "No last_sync timestamp on this account, so feed health is "
                    "inferred from transaction age — a weaker signal. Declare it "
                    "'manual' in config if it is not bank-linked."
                )
                if days is None or days > threshold * 3:
                    row["status"] = "dead"
                elif days > threshold:
                    row["status"] = "stale"
                else:
                    row["status"] = "fresh"
            rows.append(row)

        rows.sort(key=lambda r: (-(r["days_since_last_transaction"] or 10_000), r["account"]))
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
            # Healthy feed, nothing posted lately. Normal for a dormant card —
            # surfaced separately so it never reads as a sync failure.
            "quiet_but_healthy": [
                r["account"]
                for r in rows
                if r["status"] == "fresh" and r["activity"] in ("quiet", "none")
            ],
            "accounts": rows,
            # Reported separately so they are visibly excluded rather than
            # quietly missing from the list.
            "manual_accounts": manual,
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
            rec_cfg = cfg.get("recurring") or {}
            cadence_of: dict[str, str] = dict(((cfg.get("sync") or {}).get("accounts")) or {})
            txns = await _transactions(first, last)
        except ToolError as err:
            return err.payload()

        default_pct = float(rec_cfg.get("default_tolerance_pct", 10.0))
        min_tol = float(rec_cfg.get("min_tolerance", 5.0))
        items = rec_cfg.get("items") or []
        in_progress = first <= date.today() <= last

        def _tolerance(item: dict[str, Any], expected: float) -> tuple[float, float]:
            """(tolerance_dollars, pct_used) for an item."""
            pct = float(item.get("tolerance_pct", default_pct))
            return max(abs(expected) * pct / 100.0, min_tol), pct

        # Accounts whose feed is a monthly statement drop. An obligation on one
        # of these has simply not been reported yet between drops — that is not
        # the same as unpaid, and calling it MISSING every month is what trained
        # the alert away.
        monthly_accounts = {n.lower() for n, c in cadence_of.items() if c == "monthly"}
        accounts_with_activity = {
            (t.get("account_name") or "").lower() for t in txns if t.get("account_name")
        }

        def _awaiting_statement(acct_filter: list[str]) -> bool:
            """True if this item lives on a monthly account that hasn't reported yet."""
            if not in_progress or not acct_filter:
                return False
            return any(
                any(a in account for a in acct_filter) and account not in accounts_with_activity
                for account in monthly_accounts
            )

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
            tol, pct_used = _tolerance(item, expected)
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
                # On a monthly-statement account with no activity yet this
                # month, the charge hasn't been REPORTED — which is not
                # evidence it wasn't paid. Only say MISSING once the statement
                # has actually dropped, or the month is over.
                pending = _awaiting_statement(acct_filter)
                rows.append(
                    {
                        "name": name,
                        "status": "PENDING_STATEMENT" if pending else "MISSING",
                        "expected_amount": expected,
                        "expected_day": item.get("expected_day"),
                        **(
                            {
                                "note": (
                                    "This account reports on a monthly statement export; "
                                    "nothing has posted yet this cycle. Not evidence of "
                                    "a missed payment."
                                )
                            }
                            if pending
                            else {}
                        ),
                    }
                )
                continue

            claimed.add(best["id"])
            actual_amt = best["_amt"]
            delta = round(actual_amt - expected, 2)
            delta_pct = round((delta / expected * 100.0), 1) if expected else None
            within = abs(delta) <= tol
            posted = date.fromisoformat(best["date"])
            # A widened band must never mean a silent price rise: anything past
            # the DEFAULT band is called out even when its own override keeps
            # the status MATCHED.
            notable = delta_pct is not None and abs(delta_pct) > default_pct
            rows.append(
                {
                    "name": name,
                    "status": "MATCHED" if within else "CHANGED",
                    "expected_amount": expected,
                    "actual_amount": actual_amt,
                    "delta": delta,
                    "delta_pct": delta_pct,
                    "tolerance": round(tol, 2),
                    "tolerance_pct": pct_used,
                    "notable_variance": notable,
                    "expected_day": item.get("expected_day"),
                    "posted_date": best["date"],
                    "posted_day": posted.day,
                    "account": best.get("account_name"),
                    "payee": best.get("payee_name"),
                }
            )

        order = {"MISSING": 0, "CHANGED": 1, "PENDING_STATEMENT": 2, "MATCHED": 3, "ENDED": 4}
        rows.sort(key=lambda r: (order.get(str(r["status"]), 9), str(r["name"])))
        counts: dict[str, int] = defaultdict(int)
        for r in rows:
            counts[str(r["status"])] += 1
        # Sorted by absolute percentage move so the biggest surprise leads.
        notable_variances = sorted(
            (
                {
                    "name": r["name"],
                    "expected_amount": r["expected_amount"],
                    "actual_amount": r["actual_amount"],
                    "delta": r["delta"],
                    "delta_pct": r["delta_pct"],
                    "status": r["status"],
                }
                for r in rows
                if r.get("notable_variance")
            ),
            key=lambda r: -abs(float(r["delta_pct"] or 0)),
        )
        return {
            "month": target,
            "month_in_progress": in_progress,
            "counts": dict(counts),
            "needs_attention": [r["name"] for r in rows if r["status"] in ("MISSING", "CHANGED")],
            # Charges that moved more than the default band, INCLUDING ones a
            # per-item override kept at MATCHED. The seasonal-bill escape hatch
            # must not double as a way to hide a price rise.
            "notable_variances": notable_variances,
            "default_tolerance_pct": default_pct,
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
            "The debt scoreboard. Reports EVERY liability — mortgage, HELOC, car "
            "loan, financed purchases and cards — including off-budget ones, "
            "each with its balance, rate, and change over the last 7 and 30 "
            "days. Classifies each debt as 'accelerate' (rate above the "
            "configured hurdle, worth paying down early) or 'ride' (cheap money, "
            "pay on schedule), and totals each group. Loudly flags any debt whose "
            "classification changed since the last run — a variable-rate debt "
            "crossing the hurdle must never pass unnoticed. Also reports home "
            "equity (house minus mortgage minus HELOC) and flags cards carrying "
            "a balance late in the cycle. Use for the pulse's debt line and any "
            "deleveraging discussion. Equity is display-only, never an "
            "affordability input. A debt whose rate is not configured is "
            "classified 'unknown' rather than guessed at."
        ),
    )
    async def debt_status() -> dict[str, Any]:
        try:
            cfg = _config()
            hurdle = float(cfg.get("hurdle_rate", 5.0))
            debt_cfg: dict[str, Any] = {
                k: v for k, v in (cfg.get("debts") or {}).items() if not k.startswith("_")
            }
            today = date.today()
            accounts = await _accounts()
            txns = await _transactions(today - timedelta(days=31), today)
        except ToolError as err:
            return err.payload()

        def _is_account_setup(t: dict[str, Any]) -> bool:
            """True for Actual's opening-balance entry, which isn't debt movement.

            Linking an account writes a single 'Starting Balance' transaction
            for its whole balance. Counted as activity, a newly-linked loan
            reads as the household taking on the entire debt this month — the
            Tesla loan showed a $43,685.88 30-day 'change' the day it was added.
            """
            return (t.get("payee_name") or "").strip().lower() == "starting balance" or (
                t.get("category_name") or ""
            ).strip().lower() == "starting balances"

        # Reconstruct historical balances by unwinding recent activity from the
        # current balance. Actual has no as-of-date balance API, and deriving it
        # this way stays correct regardless of when the account last synced.
        def balance_as_of(account_id: str, current_cents: int, cutoff: date) -> int:
            delta = sum(
                int(t["amount_cents"])
                for t in txns
                if t["account_id"] == account_id
                and date.fromisoformat(t["date"]) > cutoff
                and not _is_account_setup(t)
            )
            return current_cents - delta

        open_accounts = [a for a in accounts if not a["closed"]]
        by_name = {a["name"]: a for a in open_accounts}

        def _find(needle: str) -> dict[str, Any] | None:
            """The one open account whose name contains `needle`, else None."""
            hits = [a for a in open_accounts if needle in a["name"].lower()]
            return hits[0] if len(hits) == 1 else None

        debts: list[dict[str, Any]] = []
        unlisted: list[dict[str, Any]] = []
        cards_flagged: list[str] = []

        for a in open_accounts:
            cents = a["balance_cents"]
            if cents >= 0:
                continue  # asset side — never debt, whatever the config says
            meta = debt_cfg.get(a["name"])
            if meta is None:
                # A negative balance we were not told about. Reported rather
                # than dropped: silently omitting a liability is precisely the
                # defect that hid the Tesla loan.
                unlisted.append({"account": a["name"], "balance": _d(cents)})
                continue

            raw_rate = meta.get("rate")
            rate = float(raw_rate) if raw_rate is not None else None
            klass = _classify(rate, hurdle)

            b7 = balance_as_of(a["id"], cents, today - timedelta(days=7))
            b30 = balance_as_of(a["id"], cents, today - timedelta(days=30))
            row: dict[str, Any] = {
                "account": a["name"],
                "balance": _d(cents),
                "rate": rate,
                "is_variable": bool(meta.get("is_variable")),
                "scheduled_payment": meta.get("scheduled_payment"),
                "class": klass,
                "offbudget": bool(a["offbudget"]),
                # Negative change = balance moved toward zero = paydown.
                "change_7d": _d(cents - b7),
                "change_30d": _d(cents - b30),
            }
            if meta.get("note"):
                row["note"] = meta["note"]

            # "Creeping back" only means something for revolving credit, and
            # only late in the cycle — before ~day 25 an unpaid statement is
            # normal. Instalment and credit-line debt carry balances by design.
            name_l = a["name"].lower()
            revolving = not any(k in name_l for k in ("heloc", "mortgage", "loan", "synchrony"))
            if revolving and -cents > 50_000 and today.day > 25:
                row["flag"] = "balance over $500 late in the cycle — revolving may be returning"
                cards_flagged.append(a["name"])
            debts.append(row)

        debts.sort(key=lambda r: r["balance"])

        # ── classification change detection ───────────────────────────
        current_classes = {r["account"]: r["class"] for r in debts}
        state = _load_state()
        class_changes: list[dict[str, Any]] = []
        change_detection = "unavailable"
        if state is not None:
            previous = dict(state.get("classes") or {})
            prev_hurdle = state.get("hurdle_rate")
            for account, klass in sorted(current_classes.items()):
                was = previous.get(account)
                if was is not None and was != klass:
                    row = next(r for r in debts if r["account"] == account)
                    class_changes.append(
                        {
                            "account": account,
                            "was": was,
                            "now": klass,
                            "rate": row["rate"],
                            "is_variable": row["is_variable"],
                            "hurdle_rate": hurdle,
                            "previous_hurdle_rate": prev_hurdle,
                        }
                    )
            change_detection = "active" if previous else "baseline"
            if not _save_state(
                {
                    "classes": current_classes,
                    "hurdle_rate": hurdle,
                    "evaluated_on": today.isoformat(),
                }
            ):
                change_detection = "read_only"

        # ── equity ────────────────────────────────────────────────────
        # Fully derived: every input except the House valuation comes from
        # Actual. If an account can't be identified we report null and say
        # which — a silently stale number was the failure mode this replaced.
        equity: float | None = None
        equity_note = None
        house = by_name.get("House")
        mortgage = _find("mortgage")
        heloc = _find("heloc")
        missing = [
            label for label, acct in (("House", house), ("Mortgage", mortgage)) if acct is None
        ]
        if missing:
            equity_note = (
                f"Could not identify a unique {' and '.join(missing)} account in Actual; "
                "equity not computed. Check the account name, or that only one open "
                "account matches."
            )
        else:
            assert house is not None and mortgage is not None
            # Both balances are negative, so adding them subtracts the debt.
            heloc_cents = heloc["balance_cents"] if heloc else 0
            equity = round(
                _d(house["balance_cents"]) + _d(mortgage["balance_cents"]) + _d(heloc_cents),
                2,
            )

        def _total(cls: str) -> float:
            return round(sum(float(r["balance"]) for r in debts if r["class"] == cls), 2)

        unlisted_total = round(sum(float(r["balance"]) for r in unlisted), 2)
        return {
            "as_of": today.isoformat(),
            "hurdle_rate": hurdle,
            "debts": debts,
            "accelerate_total": _total("accelerate"),
            "ride_total": _total("ride"),
            "unknown_total": _total("unknown"),
            # Everything with a negative balance, listed or not, so this figure
            # can never be quietly smaller than reality.
            "total_debt": round(sum(float(r["balance"]) for r in debts) + unlisted_total, 2),
            "home_equity": equity,
            "home_equity_note": equity_note,
            "house_value": _d(house["balance_cents"]) if house else None,
            "cards_flagged": cards_flagged,
            # LOUD by design: a variable-rate debt crossing the hurdle changes
            # the household's whole payoff priority.
            "class_changes": class_changes,
            "class_change_alert": bool(class_changes),
            # active = compared against a prior run; baseline = first run, so
            # nothing to compare; read_only/unavailable = the memo could not be
            # persisted, so a future change might go undetected.
            "class_change_detection": change_detection,
            "unlisted_negative_accounts": unlisted,
            "unlisted_note": (
                "These carry a negative balance but have no entry in the config's "
                "`debts` section, so they are uncategorized and unclassified. Add "
                "them (with a rate) to bring them into the accelerate/ride split."
            )
            if unlisted
            else None,
        }
