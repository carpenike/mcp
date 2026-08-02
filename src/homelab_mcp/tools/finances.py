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
import re
from collections import defaultdict
from datetime import UTC, date, datetime, timedelta
from typing import TYPE_CHECKING, Annotated, Any

from mcp.types import ToolAnnotations
from pydantic import BaseModel, ConfigDict, Field
from pydantic import ValidationError as PydanticValidationError

from homelab_mcp.tools._http import ToolError, enc, make_client, request_json

if TYPE_CHECKING:
    import httpx
    from mcp.server.fastmcp import FastMCP

    from homelab_mcp.config import Settings

log = logging.getLogger(__name__)
# Writes are invisible to RequestLogMiddleware (it only sees POST /mcp).
audit = logging.getLogger("homelab_mcp.audit")

_RO = ToolAnnotations(
    readOnlyHint=True, destructiveHint=False, idempotentHint=True, openWorldHint=False
)
# trigger_sync=True asks Actual to pull from the banks, so this one isn't
# idempotent. openWorld stays False: we call the fixed internal sidecar, and
# the outbound bank fetch happens on the Actual server, not from here.
_RO_SYNC = ToolAnnotations(
    readOnlyHint=True, destructiveHint=False, idempotentHint=False, openWorldHint=False
)

# Categorizing converges: applying the same category twice leaves the same
# state, so it is idempotent but decidedly not read-only.
_WRITE_IDEMPOTENT = ToolAnnotations(
    readOnlyHint=False, destructiveHint=False, idempotentHint=True, openWorldHint=False
)
# Each call creates a NEW rule, so repeating it is not a no-op.
_WRITE_CREATE = ToolAnnotations(
    readOnlyHint=False, destructiveHint=False, idempotentHint=False, openWorldHint=False
)
# Removal is destructive; repeating it converges on "gone".
_WRITE_DESTRUCTIVE = ToolAnnotations(
    readOnlyHint=False, destructiveHint=True, idempotentHint=True, openWorldHint=False
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


class Assignment(BaseModel):
    """One categorization instruction.

    `extra="forbid"` is the load-bearing line. The advisor's remit is to
    categorize and annotate, never to move money, and this is what makes that
    a property of the interface rather than a rule someone has to remember:
    a caller that sends `amount`, `payee`, `account` or `date` gets a
    validation error, because those fields do not exist here at all.
    """

    model_config = ConfigDict(extra="forbid")

    transaction_id: str = Field(min_length=1, description="Existing transaction id.")
    category: str | None = Field(
        default=None,
        description="Category name or id to assign. Pass null to CLEAR the category.",
    )
    notes: str | None = Field(default=None, description="Note to set. Pass null or '' to clear it.")

    def touches(self, field: str) -> bool:
        """Whether the caller explicitly supplied `field`.

        Distinguishes "leave it alone" (omitted) from "clear it" (explicit
        null). Without this an advisor could categorize a transaction but
        never undo it, which would make a mis-categorization permanent
        through this interface.
        """
        return field in self.model_fields_set


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


def _billing_window(item: dict[str, Any], first: date, last: date) -> tuple[date, date]:
    """The date range a month's instance of an obligation may post in.

    Default is the calendar month. An item that drafts near a month boundary
    can declare `window_slip_days`, which re-anchors the window on
    `expected_day` instead:

        anchor = expected_day of this month
        window = [anchor - before, anchor + after]

    USAA Life drafts on the 28th and posts anywhere from the 28th to the 3rd
    of the following month. Under calendar months that reads as three MISSING
    months (Nov, Feb, May) and three months holding two payments — the charge
    is attributed to the month it *cleared* rather than the one it is for.

    Anchored windows for consecutive months cannot overlap as long as
    before + after stays well under a month, so a single transaction is
    claimable by exactly one month. That is what keeps a slipped payment from
    being counted twice.
    """
    slip = item.get("window_slip_days")
    day = item.get("expected_day")
    if not slip or not day:
        return first, last
    before = int(slip.get("before", 0))
    after = int(slip.get("after", 0))
    # Clamp so expected_day=31 still resolves in a 30-day month.
    anchor = first.replace(day=min(int(day), calendar.monthrange(first.year, first.month)[1]))
    return anchor - timedelta(days=before), anchor + timedelta(days=after)


def _is_account_setup(txn: dict[str, Any]) -> bool:
    """True for Actual's opening-balance entry, which is not real activity.

    Linking an account writes one 'Starting Balance' transaction for its whole
    balance. Counted as activity it reads as the household taking on the
    entire amount that month; categorized, it would land in a spend total.
    """
    return (txn.get("payee_name") or "").strip().lower() == "starting balance" or (
        txn.get("category_name") or ""
    ).strip().lower() == "starting balances"


SAVINGS_CATEGORY = "Savings/Investments"


def _lumpy_monthly(cfg: dict[str, Any]) -> tuple[float, list[dict[str, Any]]]:
    """Total 1/12 amortization of annual lumpy costs, plus the per-item detail.

    PLAN.md defines the Bonus Dependence Gap as monthly baseline spend
    *including* 1/12 of annual lumpy expenses. Without this the raw monthly
    gap flatters every month one of them does not happen to land, then
    lurches in the month it does.

    Entries with a null `annual_amount` are listed but contribute nothing — a
    not-yet-quantified cost must not become an invented one.
    """
    items = ((cfg.get("lumpy") or {}).get("items")) or []
    detail: list[dict[str, Any]] = []
    total = 0.0
    for item in items:
        amount = item.get("annual_amount")
        monthly = round(float(amount) / 12.0, 2) if amount is not None else None
        if monthly is not None:
            total += monthly
        detail.append(
            {
                "name": item.get("name"),
                "category": item.get("category"),
                "annual_amount": amount,
                "monthly_amortized": monthly,
                "quantified": amount is not None,
                "note": item.get("note"),
            }
        )
    return round(total, 2), detail


def _match_obligations(
    cfg: dict[str, Any],
    txns: list[dict[str, Any]],
    target: str,
    first: date,
    last: date,
    today: date,
) -> tuple[list[dict[str, Any]], set[str]]:
    """Match configured obligations against posted transactions.

    Lifted out of `finances_recurring` so `finances_room` can reuse the SAME
    matching rather than a copy of it — two implementations of "has the
    mortgage posted yet?" would drift, and room's arithmetic is only as
    trustworthy as its agreement with the checklist.

    Returns the obligation rows and the set of transaction ids they claimed.
    """
    rec_cfg = cfg.get("recurring") or {}
    cadence_of: dict[str, str] = dict(((cfg.get("sync") or {}).get("accounts")) or {})
    default_pct = float(rec_cfg.get("default_tolerance_pct", 10.0))
    min_tol = float(rec_cfg.get("min_tolerance", 5.0))
    items = rec_cfg.get("items") or []
    in_progress = first <= today <= last

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
        (t.get("account_name") or "").lower()
        for t in txns
        if t.get("account_name") and first.isoformat() <= t["date"] <= last.isoformat()
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
        win_start, win_end = _billing_window(item, first, last)
        win_lo, win_hi = win_start.isoformat(), win_end.isoformat()
        needles = [str(s).lower() for s in (item.get("match_any") or [])]
        acct_filter = [str(s).lower() for s in (item.get("accounts") or [])]

        best: dict[str, Any] | None = None
        for t in candidates:
            if t["id"] in claimed:
                continue
            if not (win_lo <= t["date"] <= win_hi):
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
                "billing_window": [win_lo, win_hi],
                # True when the charge cleared outside the calendar month
                # it belongs to — normal for a draft near a boundary.
                "posted_outside_month": not (first.isoformat() <= best["date"] <= last.isoformat()),
                "account": best.get("account_name"),
                "payee": best.get("payee_name"),
            }
        )

    order = {"MISSING": 0, "CHANGED": 1, "PENDING_STATEMENT": 2, "MATCHED": 3, "ENDED": 4}
    rows.sort(key=lambda r: (order.get(str(r["status"]), 9), str(r["name"])))
    return rows, claimed


def _recurring_padding(cfg: dict[str, Any]) -> tuple[int, int]:
    """(days before, days after) the calendar month any obligation may post in."""
    slips = [i.get("window_slip_days") or {} for i in (cfg.get("recurring") or {}).get("items", [])]
    return (
        max((int(x.get("before", 0)) for x in slips), default=0),
        max((int(x.get("after", 0)) for x in slips), default=0),
    )


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

    async def _post(path: str, payload: dict[str, Any]) -> Any:
        if not base:
            raise ToolError(
                "finances_not_configured",
                "The Actual sidecar is not configured.",
                "Set HOMELAB_MCP_FINANCES_SIDECAR_BASE_URL (and its token).",
            )
        return await request_json(
            client,
            "POST",
            f"{base}{path}",
            service="actual",
            json=payload,
            unreachable_hint="Check that the Actual sidecar is running.",
        )

    async def _delete(path: str) -> Any:
        if not base:
            raise ToolError(
                "finances_not_configured",
                "The Actual sidecar is not configured.",
                "Set HOMELAB_MCP_FINANCES_SIDECAR_BASE_URL (and its token).",
            )
        return await request_json(
            client,
            "DELETE",
            f"{base}{path}",
            service="actual",
            unreachable_hint="Check that the Actual sidecar is running.",
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
            "month still in progress it also returns a pro-rated pace projection, "
            "plus rolling-seven-day and month-to-date Amazon spend. "
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
            today = date.today()
            anchor = min(max(today, first), last)
            week_start = anchor - timedelta(days=6)
            cfg = _config()
            pulse_cfg = cfg.get("pulse") or {}
            categories = await _categories()
            income_names = _income_category_names(categories)
            txns = await _transactions(min(first, week_start), last)
        except ToolError as err:
            return err.payload()

        by_category: dict[str, int] = defaultdict(int)
        income_cents = 0
        uncategorized_cents = 0
        amazon_mtd_cents = 0
        amazon_week_cents = 0
        amazon_needles = [
            str(value).lower() for value in pulse_cfg.get("amazon_match_any", ["amazon", "amzn"])
        ]
        for t in txns:
            txn_date = date.fromisoformat(t["date"])
            if _is_spend(t, income_names):
                payee = str(t.get("payee_name") or "").lower()
                if any(needle in payee for needle in amazon_needles):
                    if first <= txn_date <= last:
                        amazon_mtd_cents += -t["amount_cents"]
                    if week_start <= txn_date <= anchor:
                        amazon_week_cents += -t["amount_cents"]
            if not first <= txn_date <= last:
                continue
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
        # The floor governs CONSUMPTION. A 529 or brokerage contribution is
        # wealth-building, and counting it as spend would penalize exactly the
        # behaviour the plan wants more of — so it is reported beside the gap,
        # never inside it.
        savings_cents = by_category.get(SAVINGS_CATEGORY, 0)
        consumption_cents = total_cents - savings_cents
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
            "consumption_spend": _d(consumption_cents),
            "savings_contributions": _d(savings_cents),
            "gap_basis": "consumption_spend",
            "gap_basis_note": (
                f"The floor is compared against consumption only; the "
                f"{SAVINGS_CATEGORY!r} category is reported separately rather "
                "than counted as spend."
            ),
            "uncategorized": _d(uncategorized_cents),
            "spend_by_category": [
                {"category": k, "spend": _d(v)}
                for k, v in sorted(by_category.items(), key=lambda kv: -kv[1])
            ],
            "excluded_category": EXCLUDED_CATEGORY,
            "floor": floor,
            "amazon": {
                "week_start": week_start.isoformat(),
                "week_end": anchor.isoformat(),
                "week_spend": _d(amazon_week_cents),
                "mtd_spend": _d(amazon_mtd_cents),
                "monthly_baseline": settings.finances_amazon_baseline,
            },
        }
        # Lumpy amortization: the gap is DEFINED with it (PLAN.md), so both
        # views are returned and the amortized one is named as the definition.
        lumpy_monthly, lumpy_detail = _lumpy_monthly(cfg)
        result["lumpy_monthly_amortized"] = lumpy_monthly
        result["lumpy_items"] = lumpy_detail
        result["consumption_spend_amortized"] = round(_d(consumption_cents) + lumpy_monthly, 2)

        if floor is None:
            result["gap_vs_floor"] = None
            result["gap_vs_floor_amortized"] = None
            result["gap_note"] = (
                "No floor configured (HOMELAB_MCP_FINANCES_FLOOR); gap not computed."
            )
        else:
            result["gap_vs_floor"] = round(_d(consumption_cents) - floor, 2)
            # This one is the Bonus Dependence Gap as PLAN.md defines it.
            result["gap_vs_floor_amortized"] = round(
                _d(consumption_cents) + lumpy_monthly - floor, 2
            )
            if in_progress:
                # Straight-line pace. Deliberately naive — a weighted model would
                # imply a forecasting confidence this data doesn't support.
                prorated = round(floor * days_elapsed / days_in_month, 2)
                projected = round(_d(consumption_cents) * days_in_month / days_elapsed, 2)
                result["prorated_floor_to_date"] = prorated
                result["gap_vs_prorated_floor"] = round(_d(consumption_cents) - prorated, 2)
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
            "history. Also returns genuinely new payees whose month-to-date "
            "spend exceeds the configured review threshold."
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
            new_payee_threshold = float(rec_cfg.get("new_payee_threshold", 500.0))
            new_payee_lookback_days = int(rec_cfg.get("new_payee_lookback_days", 365))
            # Fetch wide enough to cover the most boundary-slipping item; the
            # matcher narrows per item.
            pad_before, pad_after = _recurring_padding(cfg)
            history_start = min(
                first - timedelta(days=pad_before),
                first - timedelta(days=new_payee_lookback_days),
            )
            txns = await _transactions(history_start, last + timedelta(days=pad_after))
        except ToolError as err:
            return err.payload()

        rows, claimed = _match_obligations(cfg, txns, target, first, last, date.today())
        default_pct = float(rec_cfg.get("default_tolerance_pct", 10.0))
        in_progress = first <= date.today() <= last

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
        prior_payees = {
            str(t.get("payee_name") or "").strip().lower()
            for t in txns
            if t["date"] < first.isoformat() and str(t.get("payee_name") or "").strip()
        }
        new_payees: dict[str, dict[str, Any]] = {}
        for t in txns:
            payee = str(t.get("payee_name") or "").strip()
            normalized = payee.lower()
            if (
                not first.isoformat() <= t["date"] <= last.isoformat()
                or t["id"] in claimed
                or not payee
                or normalized in prior_payees
                or t["account_offbudget"]
                or t["is_transfer"]
                or t["category_name"] == EXCLUDED_CATEGORY
                or t["amount_cents"] >= 0
            ):
                continue
            row = new_payees.setdefault(
                normalized,
                {
                    "payee": payee,
                    "spend_cents": 0,
                    "transaction_count": 0,
                    "first_seen": t["date"],
                },
            )
            row["spend_cents"] += -t["amount_cents"]
            row["transaction_count"] += 1
            row["first_seen"] = min(row["first_seen"], t["date"])
        new_payees_over_threshold = sorted(
            (
                {
                    "payee": row["payee"],
                    "spend": _d(row["spend_cents"]),
                    "transaction_count": row["transaction_count"],
                    "first_seen": row["first_seen"],
                }
                for row in new_payees.values()
                if _d(row["spend_cents"]) > new_payee_threshold
            ),
            key=lambda row: (-float(row["spend"]), str(row["payee"]).lower()),
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
            "new_payee_threshold": new_payee_threshold,
            "new_payee_lookback_days": new_payee_lookback_days,
            "new_payees_over_threshold": new_payees_over_threshold,
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

    # ── 6. transactions (the read side of the iteration loop) ────────
    @mcp.tool(
        annotations=_RO,
        name="finances_transactions",
        description=(
            "List individual transactions, filtered. This is the read half of "
            "the categorization loop: call it with uncategorized_only=true to "
            "find what needs attention, then pass the ids to "
            "finances_categorize. Filters: uncategorized_only, account name, "
            "date range, payee substring, amount range (dollars, signed — "
            "spend is negative), and category name. Returns id, date, account, "
            "payee, amount, category and notes per row, plus the total match "
            "count so you can tell when you are seeing a partial answer. "
            "Defaults to the last 90 days when no date range is given."
        ),
    )
    async def transactions(
        uncategorized_only: Annotated[
            bool, Field(description="Only transactions with no category assigned.")
        ] = False,
        account: Annotated[str | None, Field(description="Account name (exact).")] = None,
        date_from: Annotated[str | None, Field(description="Earliest date, 'YYYY-MM-DD'.")] = None,
        date_to: Annotated[str | None, Field(description="Latest date, 'YYYY-MM-DD'.")] = None,
        payee_contains: Annotated[
            str | None, Field(description="Case-insensitive substring of payee or notes.")
        ] = None,
        amount_min: Annotated[
            float | None, Field(description="Minimum amount in dollars (signed).")
        ] = None,
        amount_max: Annotated[
            float | None, Field(description="Maximum amount in dollars (signed).")
        ] = None,
        category: Annotated[str | None, Field(description="Category name (exact).")] = None,
        include_account_setup: Annotated[
            bool,
            Field(description="Include 'Starting Balance' rows in an uncategorized worklist."),
        ] = False,
        limit: Annotated[int, Field(ge=1, le=200)] = 50,
    ) -> dict[str, Any]:
        try:
            today = date.today()
            start = date.fromisoformat(date_from) if date_from else today - timedelta(days=90)
            end = date.fromisoformat(date_to) if date_to else today
            rows = await _transactions(start, end)
        except ValueError:
            return ToolError(
                "finances_bad_date",
                "date_from and date_to must be 'YYYY-MM-DD'.",
                "",
            ).payload()
        except ToolError as err:
            return err.payload()

        needle = (payee_contains or "").lower()
        matched = []
        for t in rows:
            setup = _is_account_setup(t)
            # A "Starting Balance" row is how Actual records an account's
            # opening position. It is uncategorized by nature and is NOT a
            # decision anyone needs to make; worse, categorizing one on an
            # on-budget account would inject a phantom five-figure "spend"
            # into the month. Keep them out of the worklist unless asked for.
            if setup and uncategorized_only and not include_account_setup:
                continue
            if uncategorized_only and t.get("category_name"):
                continue
            if account and t.get("account_name") != account:
                continue
            if category and t.get("category_name") != category:
                continue
            if needle:
                hay = f"{t.get('payee_name') or ''} {t.get('notes') or ''}".lower()
                if needle not in hay:
                    continue
            amt = _d(t["amount_cents"])
            if amount_min is not None and amt < amount_min:
                continue
            if amount_max is not None and amt > amount_max:
                continue
            matched.append(t)

        matched.sort(key=lambda t: (t["date"], t["id"]), reverse=True)
        page = matched[:limit]
        return {
            "returned": len(page),
            "total": len(matched),
            "truncated": len(matched) > len(page),
            "date_from": start.isoformat(),
            "date_to": end.isoformat(),
            "transactions": [
                {
                    "id": t["id"],
                    "date": t["date"],
                    "account": t.get("account_name"),
                    "payee": t.get("payee_name"),
                    "amount": _d(t["amount_cents"]),
                    "category": t.get("category_name"),
                    "notes": t.get("notes"),
                    # Flagged rather than hidden: an advisor should not
                    # categorize these, and should know why they look odd.
                    "account_setup": _is_account_setup(t),
                }
                for t in page
            ],
        }

    # ── 7. categorize (the write side) ───────────────────────────────
    @mcp.tool(
        annotations=_WRITE_IDEMPOTENT,
        name="finances_categorize",
        description=(
            "Assign categories (and optionally notes) to existing transactions, "
            "in one batch. Each assignment takes a transaction_id, a category "
            "name or id, and optional notes — those are the ONLY fields it can "
            "change. It cannot create or delete a transaction, and cannot touch "
            "an amount, payee, account or date; there is no parameter for them. "
            "Unknown category names are rejected with the list of valid ones "
            "rather than guessed at. The whole batch is committed with a single "
            "sync, so prefer one call with many assignments over many calls. "
            "Returns a per-assignment result so a partial failure is visible."
        ),
    )
    async def categorize(
        assignments: Annotated[
            list[Assignment],
            Field(description="Up to 200 {transaction_id, category, notes?} items."),
        ],
    ) -> dict[str, Any]:
        try:
            if not assignments:
                raise ToolError(
                    "finances_empty_batch", "No assignments supplied.", "Pass at least one."
                )
            if len(assignments) > 200:
                raise ToolError(
                    "finances_batch_too_large",
                    f"{len(assignments)} assignments; the limit is 200.",
                    "Split the work across calls.",
                )
            # Re-validate at the tool boundary rather than trusting the
            # transport to have done it. This is what makes "cannot touch an
            # amount" a property of the tool itself: an unexpected key raises
            # here even if a client bypassed the JSON-schema layer.
            try:
                items = [
                    a if isinstance(a, Assignment) else Assignment.model_validate(a)
                    for a in assignments
                ]
            except PydanticValidationError as exc:
                bad = sorted(
                    {str(e["loc"][-1]) for e in exc.errors() if e.get("type") == "extra_forbidden"}
                )
                raise ToolError(
                    "finances_forbidden_field",
                    (
                        f"Assignments may not set {', '.join(bad)}."
                        if bad
                        else "Invalid assignment."
                    ),
                    "Only transaction_id, category and notes may be set. This tool "
                    "cannot change an amount, payee, account or date.",
                ) from exc

            cats = await _categories()
            by_name = {c["name"]: c["id"] for c in cats}
            by_id = {c["id"]: c["name"] for c in cats}

            resolved: list[dict[str, Any]] = []
            for a in items:
                if not (a.touches("category") or a.touches("notes")):
                    raise ToolError(
                        "finances_nothing_to_set",
                        f"Assignment for {a.transaction_id} sets neither category nor notes.",
                        "Supply category (or null to clear) and/or notes.",
                    )
                item: dict[str, Any] = {"transaction_id": a.transaction_id}
                if a.touches("category"):
                    if a.category is None:
                        item["category_id"] = None  # explicit clear
                    else:
                        cat_id = by_name.get(a.category) or (
                            a.category if a.category in by_id else None
                        )
                        if cat_id is None:
                            raise ToolError(
                                "finances_unknown_category",
                                f"No category named {a.category!r}.",
                                "Valid categories: " + ", ".join(sorted(by_name)),
                            )
                        item["category_id"] = cat_id
                if a.touches("notes"):
                    item["notes"] = a.notes
                resolved.append(item)

            data = await _post("/transactions/categorize", {"assignments": resolved})
        except ToolError as err:
            return err.payload()

        results = list((data or {}).get("results") or [])
        for r, a in zip(results, items, strict=False):
            r["category"] = a.category
        ok = sum(1 for r in results if r.get("ok"))
        audit.info("finances_categorize applied=%d failed=%d", ok, len(results) - ok)
        return {
            "applied": ok,
            "failed": len(results) - ok,
            "synced": bool((data or {}).get("changed")),
            "results": results,
        }

    # ── 8. rules ─────────────────────────────────────────────────────
    def _shape_rule(r: dict[str, Any], cat_names: dict[str, str]) -> dict[str, Any]:
        targets = [
            cat_names.get(str(a.get("value")), str(a.get("value")))
            for a in (r.get("actions") or [])
            if a.get("field") == "category"
        ]
        return {
            "id": r.get("id"),
            "conditions": r.get("conditions"),
            "conditions_op": r.get("conditions_op"),
            "sets_category": targets[0] if targets else None,
            "sets_category_only": r.get("sets_category_only"),
        }

    @mcp.tool(
        annotations=_RO,
        name="finances_rules_list",
        description=(
            "List Actual's auto-categorization rules: what each one matches and "
            "which category it assigns. Use before creating a rule, to avoid "
            "duplicating one that already covers the payee. Rules this server "
            "did not create may do more than set a category; those are marked "
            "sets_category_only=false and cannot be deleted through here."
        ),
    )
    async def rules_list() -> dict[str, Any]:
        try:
            cats = await _categories()
            data = await _get("/rules")
        except ToolError as err:
            return err.payload()
        names = {c["id"]: c["name"] for c in cats}
        rules = [_shape_rule(r, names) for r in (data or {}).get("rules") or []]
        return {"returned": len(rules), "total": len(rules), "truncated": False, "rules": rules}

    @mcp.tool(
        annotations=_WRITE_CREATE,
        name="finances_rule_create",
        description=(
            "Create an auto-categorization rule that assigns one category. "
            "Match EITHER by payee ids (exact, from finances_transactions) OR "
            "by a regular expression against the raw imported payee string — "
            "give exactly one. The rule's only effect is setting the category; "
            "it cannot rewrite payees or alter amounts, because the action is "
            "constructed server-side and no action parameter is exposed. Prefer "
            "a rule over repeated manual categorization when a payee recurs."
        ),
    )
    async def rule_create(
        category: Annotated[str, Field(description="Category name or id to assign.")],
        payee_ids: Annotated[
            list[str] | None, Field(description="Exact payee ids to match.")
        ] = None,
        imported_payee_regex: Annotated[
            str | None, Field(description="Regex matched against the raw imported payee.")
        ] = None,
    ) -> dict[str, Any]:
        try:
            if bool(payee_ids) == bool(imported_payee_regex):
                raise ToolError(
                    "finances_rule_bad_match",
                    "Give exactly one of payee_ids or imported_payee_regex.",
                    "",
                )
            if imported_payee_regex:
                try:
                    re.compile(imported_payee_regex)
                except re.error as exc:
                    raise ToolError(
                        "finances_rule_bad_regex",
                        f"Invalid regular expression: {exc}",
                        "",
                    ) from exc

            cats = await _categories()
            by_name = {c["name"]: c["id"] for c in cats}
            by_id = {c["id"]: c["name"] for c in cats}
            cat_id = by_name.get(category) or (category if category in by_id else None)
            if cat_id is None:
                raise ToolError(
                    "finances_unknown_category",
                    f"No category named {category!r}.",
                    "Valid categories: " + ", ".join(sorted(by_name)),
                )

            # Shapes copied from the rules already live in this budget: an id
            # match carries a list, a regex match carries a string.
            condition: dict[str, Any]
            if payee_ids:
                condition = {"op": "oneOf", "field": "payee", "value": payee_ids, "type": "id"}
            else:
                condition = {
                    "op": "matches",
                    "field": "imported_payee",
                    "value": str(imported_payee_regex),
                    "type": "string",
                }
            data = await _post(
                "/rules", {"conditions": [condition], "category_id": cat_id, "conditions_op": "and"}
            )
        except ToolError as err:
            return err.payload()

        rule = (data or {}).get("rule") or {}
        audit.info("finances_rule_create id=%s category=%s", rule.get("id"), category)
        return {"created": True, "rule_id": rule.get("id"), "sets_category": category}

    @mcp.tool(
        annotations=_WRITE_DESTRUCTIVE,
        name="finances_rule_delete",
        description=(
            "Delete an auto-categorization rule by id (from finances_rules_list). "
            "Refuses to delete a rule that does anything beyond setting a "
            "category, so a rule with side effects this server can't reason "
            "about has to be removed in Actual's own UI. Deleting a rule does "
            "not re-categorize transactions it already applied to."
        ),
    )
    async def rule_delete(
        rule_id: Annotated[str, Field(min_length=1, description="Rule id to delete.")],
    ) -> dict[str, Any]:
        try:
            data = await _get("/rules")
            existing = {r.get("id"): r for r in (data or {}).get("rules") or []}
            rule = existing.get(rule_id)
            if rule is None:
                raise ToolError(
                    "finances_rule_not_found",
                    f"No rule with id {rule_id!r}.",
                    "Call finances_rules_list for current ids.",
                )
            if not rule.get("sets_category_only"):
                raise ToolError(
                    "finances_rule_not_deletable",
                    "That rule does more than set a category.",
                    "Remove it in Actual's UI, where its full effect is visible.",
                )
            await _delete(f"/rules/{enc(rule_id)}")
        except ToolError as err:
            return err.payload()
        audit.info("finances_rule_delete id=%s", rule_id)
        return {"deleted": True, "rule_id": rule_id}

    # ── 9. payees + merge ────────────────────────────────────────────
    @mcp.tool(
        annotations=_RO,
        name="finances_payees",
        description=(
            "List payees with how many transactions each has. Use it to spot "
            "near-duplicate variants of the same merchant — the ledger carries "
            "Costco twice, Spotify twice, Starlink twice and Monrovia four "
            "times — which split a merchant's history across several names and "
            "quietly understate every per-merchant figure. Feed the ids to "
            "finances_payee_merge. Transfer payees (the other side of an "
            "account-to-account move) are flagged and must never be merged."
        ),
    )
    async def payees(
        name_contains: Annotated[
            str | None, Field(description="Case-insensitive substring filter.")
        ] = None,
        min_transactions: Annotated[int, Field(ge=0)] = 0,
        limit: Annotated[int, Field(ge=1, le=500)] = 200,
    ) -> dict[str, Any]:
        try:
            data = await _get("/payees")
        except ToolError as err:
            return err.payload()
        rows = [
            {
                "id": p["id"],
                "name": p["name"],
                "transaction_count": p.get("transaction_count", 0),
                "is_transfer": bool(p.get("transfer_acct")),
            }
            for p in (data or {}).get("payees") or []
        ]
        needle = (name_contains or "").lower()
        if needle:
            rows = [r for r in rows if needle in (r["name"] or "").lower()]
        rows = [r for r in rows if r["transaction_count"] >= min_transactions]
        rows.sort(key=lambda r: (-r["transaction_count"], (r["name"] or "").lower()))
        page = rows[:limit]
        return {
            "returned": len(page),
            "total": len(rows),
            "truncated": len(rows) > len(page),
            "payees": page,
        }

    @mcp.tool(
        annotations=_WRITE_DESTRUCTIVE,
        name="finances_payee_merge",
        description=(
            "Merge duplicate payees into one. Every transaction pointing at a "
            "merged payee is repointed at keep_id and the merged payees are "
            "deleted. IRREVERSIBLE — Actual has no undo for this; recovering "
            "means restoring a backup. Confirm the ids with finances_payees "
            "first, and merge only true variants of the same merchant. It "
            "cannot rename anything: the surviving payee keeps keep_id's name, "
            "so choose the spelling you want by choosing which id to keep. "
            "Refuses to touch transfer payees, where a merge would corrupt the "
            "account-to-account wiring."
        ),
    )
    async def payee_merge(
        keep_id: Annotated[str, Field(min_length=1, description="Payee id to keep.")],
        merge_ids: Annotated[
            list[str], Field(description="Payee ids to fold into keep_id and delete.")
        ],
    ) -> dict[str, Any]:
        try:
            if not merge_ids:
                raise ToolError("finances_merge_empty", "merge_ids is empty.", "")
            if keep_id in merge_ids:
                raise ToolError(
                    "finances_merge_self",
                    "keep_id also appears in merge_ids.",
                    "The surviving payee cannot also be merged away.",
                )
            data = await _get("/payees")
            known = {p["id"]: p for p in (data or {}).get("payees") or []}
            missing = [i for i in [keep_id, *merge_ids] if i not in known]
            if missing:
                raise ToolError(
                    "finances_payee_not_found",
                    f"Unknown payee id(s): {', '.join(missing)}.",
                    "Call finances_payees for current ids.",
                )
            transfers = [i for i in [keep_id, *merge_ids] if known[i].get("transfer_acct")]
            if transfers:
                raise ToolError(
                    "finances_merge_transfer_payee",
                    "One or more ids is a transfer payee: "
                    + ", ".join(known[i]["name"] for i in transfers),
                    "Transfer payees are the other side of an account-to-account "
                    "move; merging one would corrupt that wiring.",
                )
            moved = sum(known[i].get("transaction_count", 0) for i in merge_ids)
            await _post("/payees/merge", {"keep_id": keep_id, "merge_ids": merge_ids})
        except ToolError as err:
            return err.payload()
        audit.warning(
            "finances_payee_merge keep=%s merged=%s transactions_moved=%d",
            keep_id,
            ",".join(merge_ids),
            moved,
        )
        return {
            "merged": True,
            "kept": {"id": keep_id, "name": known[keep_id]["name"]},
            "merged_payees": [
                {
                    "id": i,
                    "name": known[i]["name"],
                    "transactions": known[i].get("transaction_count", 0),
                }
                for i in merge_ids
            ],
            "transactions_repointed": moved,
            "reversible": False,
        }

    # ── 10. buffer ───────────────────────────────────────────────────
    @mcp.tool(
        annotations=_RO,
        name="finances_buffer",
        description=(
            "The buffer: cleared USAA Checking, minus every revolving card "
            "balance, minus the next mortgage payment. 'Could we clear the "
            "decks today, from the operating account alone?' Checking ONLY — "
            "no HYSA, no brokerage, because operations run on checking. Uses "
            "CLEARED balances: money the bank hasn't confirmed can't clear "
            "anything. Also returns the buffer after the next 14 days of "
            "scheduled outflows, so an autopay pull is never a surprise. "
            "Returns every component alongside the totals so the number can be "
            "checked by hand. Status is measured against the configured buffer "
            "floor, or 'no_floor' when none is set."
        ),
    )
    async def buffer() -> dict[str, Any]:
        try:
            cfg = _config()
            bcfg = dict(cfg.get("buffer") or {})
            cash_names = list(bcfg.get("cash_accounts") or [])
            card_names = list(bcfg.get("card_accounts") or [])
            lookahead = int(bcfg.get("lookahead_days", 14))
            accounts = await _accounts()
        except ToolError as err:
            return err.payload()

        # Cash is what the BANK says is available today. The cleared register
        # counts money already committed to in-flight payments as spendable —
        # reconcile measured ~$10k of that optimism on 2026-08-01 — and pairing
        # cleared-basis cash with register-basis cards double-counts a payment
        # mid-settlement. Bank-available cash + register cards is
        # double-count-proof in both directions.
        bank_by_id: dict[str, dict[str, Any]] = {}
        degraded_reason: str | None = None
        try:
            payload = await _get("/simplefin-balances")
            for row in (payload or {}).get("accounts") or []:
                if row.get("simplefin_id"):
                    bank_by_id[str(row["simplefin_id"])] = row
        except ToolError as err:
            degraded_reason = err.message

        by_name = {a["name"]: a for a in accounts if not a["closed"]}
        cash_detail: list[dict[str, Any]] = []
        for n in cash_names:
            a = by_name.get(n)
            if a is None:
                continue
            bank_row = bank_by_id.get(str(a.get("simplefin_id") or ""))
            available = None
            if bank_row is not None:
                # available-balance is the honest "today" figure; some feeds
                # only carry `balance`.
                available = bank_row.get("available_balance")
                if available is None:
                    available = bank_row.get("balance")
            cleared = _d(a["cleared_balance_cents"])
            cash_detail.append(
                {
                    "account": n,
                    "balance_used": round(float(available), 2)
                    if available is not None
                    else cleared,
                    "basis": "bank_available" if available is not None else "cleared_register",
                    "bank_available_balance": (
                        round(float(available), 2) if available is not None else None
                    ),
                    "cleared_balance": cleared,
                }
            )
        cash = round(sum(float(c["balance_used"]) for c in cash_detail), 2)
        fell_back = [c["account"] for c in cash_detail if c["basis"] == "cleared_register"]

        # Only balances that are OWED reduce the buffer. A card carrying a
        # credit is not cash on hand.
        # Cards use the REGISTER balance, not the cleared one — PLAN.md
        # qualifies only the cash side as cleared ("cleared USAA Checking -
        # all card balances"). It is also the conservative reading: a card
        # payment already made but not yet settled has left the obligation
        # behind it, and counting the pre-payment cleared balance against
        # cleared cash that still holds the money would double-count it.
        card_detail = []
        for n in card_names:
            a = by_name.get(n)
            if a is None:
                continue
            bal = _d(a["balance_cents"])
            card_detail.append(
                {
                    "account": n,
                    "balance": bal,
                    "cleared_balance": _d(a["cleared_balance_cents"]),
                    "counted": bal < 0,
                }
            )
        card_debt = round(-sum(c["balance"] for c in card_detail if c["counted"]), 2)

        rec_items = ((cfg.get("recurring") or {}).get("items")) or []
        mortgage_name = bcfg.get("mortgage_obligation")
        mortgage = next(
            (float(i.get("amount", 0.0)) for i in rec_items if i.get("name") == mortgage_name),
            0.0,
        )

        value = round(cash - card_debt - mortgage, 2)

        # Look-ahead: everything the config says is due in the next N days,
        # plus the cards themselves as obligations already incurred.
        today = date.today()
        horizon = today + timedelta(days=lookahead)
        scheduled: list[dict[str, Any]] = []
        for item in rec_items:
            day = item.get("expected_day")
            amount = item.get("amount")
            if not day or amount is None or item.get("name") == mortgage_name:
                continue
            for base in (today.replace(day=1), _shift_month(today, -1)):
                try:
                    due = base.replace(
                        day=min(int(day), calendar.monthrange(base.year, base.month)[1])
                    )
                except ValueError:  # pragma: no cover - defensive
                    continue
                if today <= due <= horizon:
                    scheduled.append(
                        {"name": item.get("name"), "due": due.isoformat(), "amount": float(amount)}
                    )
                    break
        scheduled.sort(key=lambda r: r["due"])
        scheduled_total = round(sum(r["amount"] for r in scheduled), 2)

        floor = settings.finances_buffer_floor
        if floor is None:
            status = "no_floor"
        elif value < floor:
            status = "below_floor"
        elif value < floor * 1.25:
            status = "near_floor"
        else:
            status = "above_floor"

        mode = "cleared_register" if fell_back else "bank_available"
        result: dict[str, Any] = {
            "as_of": today.isoformat(),
            "buffer": value,
            "mode": mode,
            "components": {
                "cash": cash,
                "cash_accounts": cash_detail,
                "card_debt": card_debt,
                "card_accounts": card_detail,
                "next_mortgage_payment": mortgage,
            },
            "lookahead_days": lookahead,
            "scheduled_outflows": scheduled,
            "scheduled_outflows_total": scheduled_total,
            "buffer_after_scheduled": round(value - scheduled_total, 2),
            "floor": floor,
            "status": status,
            "basis": (
                "Cash is the bank's own AVAILABLE balance — what could actually "
                "be spent today, net of anything already in flight. Cards use "
                "their register balance, so a payment already made counts. That "
                "pairing cannot double-count in either direction. Checking only "
                "— HYSA, brokerage and every other account are excluded by "
                "design. Only cards in debt are counted; a card carrying a "
                "credit is not cash on hand."
            ),
        }
        if fell_back:
            result["limitation"] = (
                "The bank-reported balance was unavailable for "
                + ", ".join(fell_back)
                + f", so the cleared register was used instead ({degraded_reason or 'no bank row'})."
                " That counts money already committed to in-flight payments as"
                " available, so this buffer may read HIGH. Re-run once the feed"
                " is reachable."
            )
        return result

    # ── 11. breaches ─────────────────────────────────────────────────
    @mcp.tool(
        annotations=_RO,
        name="finances_breaches",
        description=(
            "Find deposits into checking that are not income — money arriving "
            "from the HYSA, the brokerage or the HELOC to cover operational "
            "spending. PLAN.md's operational bright line is that operations run "
            "on checking alone, so anything else landing there is a breach "
            "candidate, not a judgment call: the May 2026 $30.7k brokerage sale "
            "is the canonical case. Payees on the configured income allowlist "
            "(payroll, tax refunds, interest, healthcare reimbursements) are "
            "excluded. DETECTION ONLY — it never clears, dismisses or explains "
            "one away; that is the household's call."
        ),
    )
    async def breaches(
        lookback_days: Annotated[int, Field(ge=1, le=730)] = 30,
    ) -> dict[str, Any]:
        try:
            cfg = _config()
            allow = [
                str(x).lower()
                for x in ((cfg.get("income_payees") or {}).get("allow_substrings") or [])
            ]
            cash_names = set((cfg.get("buffer") or {}).get("cash_accounts") or [])
            today = date.today()
            rows = await _transactions(today - timedelta(days=lookback_days), today)
        except ToolError as err:
            return err.payload()

        candidates = []
        for t in rows:
            if t.get("account_name") not in cash_names:
                continue
            if t["amount_cents"] <= 0:
                continue  # outflow, not a deposit
            if _is_account_setup(t):
                continue
            hay = f"{t.get('payee_name') or ''} {t.get('notes') or ''}".lower()
            if any(a in hay for a in allow):
                continue
            candidates.append(
                {
                    "date": t["date"],
                    "amount": _d(t["amount_cents"]),
                    "payee": t.get("payee_name"),
                    "account": t.get("account_name"),
                    "notes": t.get("notes"),
                    "is_transfer": t.get("is_transfer", False),
                }
            )
        candidates.sort(key=lambda r: (-float(r["amount"]), r["date"]))
        return {
            "as_of": today.isoformat(),
            "lookback_days": lookback_days,
            "returned": len(candidates),
            "total": len(candidates),
            "truncated": False,
            "breach_candidates": candidates,
            "total_amount": round(sum(float(c["amount"]) for c in candidates), 2),
            "note": (
                "Candidates, not verdicts. Each needs an explanation; a bonus or "
                "reimbursement the allowlist doesn't know about will appear here."
            ),
        }

    # ── 12. room ─────────────────────────────────────────────────────
    @mcp.tool(
        annotations=_RO,
        name="finances_room",
        description=(
            "The 'can we spend this?' arithmetic, computed rather than "
            "estimated: how far into the month we are, month-to-date "
            "consumption against the pro-rated floor, whether that is ahead or "
            "behind pace, and how much of the floor is left. Optionally pass a "
            "category to get its month-to-date against its own trailing "
            "6-month average as a second lens. Savings/investment contributions "
            "are excluded — the floor governs consumption, not wealth-building. "
            "Returns numbers ONLY; how to phrase the answer is the caller's "
            "job. Use this rather than doing the arithmetic yourself."
        ),
    )
    async def room(
        category: Annotated[
            str | None, Field(description="Optional category for a second lens.")
        ] = None,
    ) -> dict[str, Any]:
        try:
            cfg = _config()
            today = date.today()
            month = today.strftime("%Y-%m")
            first, last = _month_bounds(month)
            _, pad_after = _recurring_padding(cfg)
            categories = await _categories()
            income_names = _income_category_names(categories)
            # Six prior months for the trailing average, and far enough past
            # month-end to cover a boundary-slipping obligation.
            rows = await _transactions(_shift_month(today, 6), last + timedelta(days=pad_after))
        except ToolError as err:
            return err.payload()

        # Same matcher finances_recurring uses — not a copy of it.
        obligations, _ = _match_obligations(cfg, rows, month, first, last, today)

        # Bucketed by month so the trailing figures below run on exactly the
        # same consumption basis as the current month, rather than a second
        # definition of "variable spend" that could drift from this one.
        consumption_by_month: dict[str, int] = defaultdict(int)
        consumption_ids_by_month: dict[str, set[str]] = defaultdict(set)
        savings_mtd = 0
        per_month_cat: dict[str, int] = defaultdict(int)
        cat_mtd = 0
        this_month = first.strftime("%Y-%m")
        for t in rows:
            if not _is_spend(t, income_names):
                continue
            bucket = t["date"][:7]
            in_month = first.isoformat() <= t["date"] <= last.isoformat()
            name = t["category_name"] or "(uncategorized)"
            amount = -t["amount_cents"]
            if name == SAVINGS_CATEGORY:
                if in_month:
                    savings_mtd += amount
            else:
                consumption_by_month[bucket] += amount
                consumption_ids_by_month[bucket].add(t["id"])
            if category and name == category:
                if in_month:
                    cat_mtd += amount
                else:
                    per_month_cat[bucket] += amount

        mtd = consumption_by_month.get(this_month, 0)
        consumption_ids = consumption_ids_by_month.get(this_month, set())

        # Which obligations bear on the CONSUMPTION floor at all. A debt-service
        # leg on a liability account (the Synchrony instalments) posts as a
        # transfer, never as consumption spend, so counting it as committed
        # would understate room against a floor it never touches.
        items_by_name = {
            str(i.get("name")): i for i in (cfg.get("recurring") or {}).get("items", [])
        }

        def _hits_floor(
            row: dict[str, Any], item: dict[str, Any], ids: set[str] | None = None
        ) -> bool:
            txn_id = row.get("transaction_id")
            if txn_id is not None:
                return txn_id in (consumption_ids if ids is None else ids)
            # Not yet posted: infer from the item. Items pinned to a specific
            # (liability) account are matched on their transfer leg.
            return not item.get("accounts")

        committed: list[dict[str, Any]] = []
        fixed_expected_total = 0.0
        fixed_posted = 0.0
        excluded: list[str] = []
        for row in obligations:
            if row["status"] == "ENDED":
                continue
            item = items_by_name.get(str(row["name"])) or {}
            if not _hits_floor(row, item):
                excluded.append(str(row["name"]))
                continue
            expected = float(row.get("expected_amount") or 0.0)
            fixed_expected_total += expected
            if row["status"] in ("MISSING", "PENDING_STATEMENT"):
                committed.append(
                    {
                        "name": row["name"],
                        "expected_amount": expected,
                        "expected_day": row.get("expected_day"),
                        "status": row["status"],
                    }
                )
            else:
                fixed_posted += float(row.get("actual_amount") or 0.0)

        committed.sort(key=lambda r: (r["expected_day"] or 99, -float(r["expected_amount"])))
        remaining_committed = round(sum(float(c["expected_amount"]) for c in committed), 2)
        fixed_expected_total = round(fixed_expected_total, 2)
        fixed_posted = round(fixed_posted, 2)

        def _variable_daily(month_start: date) -> float | None:
            """Average daily VARIABLE spend for a complete prior month.

            Runs the same obligation matcher and the same consumption/fixed
            split as the current month, so `typical_remaining_pace` is
            comparable to `required_remaining_pace` rather than a differently
            derived number that happens to share a name.
            """
            label = month_start.strftime("%Y-%m")
            if label not in consumption_by_month:
                return None
            m_last = date(
                month_start.year,
                month_start.month,
                calendar.monthrange(month_start.year, month_start.month)[1],
            )
            m_rows, _ = _match_obligations(cfg, rows, label, month_start, m_last, today)
            ids = consumption_ids_by_month.get(label, set())
            fixed = 0.0
            for r in m_rows:
                if r["status"] in ("MATCHED", "CHANGED") and _hits_floor(
                    r, items_by_name.get(str(r["name"])) or {}, ids
                ):
                    fixed += float(r.get("actual_amount") or 0.0)
            variable = _d(consumption_by_month[label]) - fixed
            return round(variable / ((m_last - month_start).days + 1), 2)

        trailing_dailies = [
            d
            for d in (_variable_daily(_shift_month(today, i)) for i in range(1, 7))
            if d is not None
        ]
        typical_remaining_pace = (
            round(sum(trailing_dailies) / len(trailing_dailies), 2) if trailing_dailies else None
        )

        days_in_month = (last - first).days + 1
        days_elapsed = (today - first).days + 1
        elapsed_pct = round(days_elapsed / days_in_month * 100.0, 1)
        floor = settings.finances_floor
        variable_mtd = round(_d(mtd) - fixed_posted, 2)

        result: dict[str, Any] = {
            "as_of": today.isoformat(),
            "month": month,
            "days_elapsed": days_elapsed,
            "days_in_month": days_in_month,
            "days_remaining": days_in_month - days_elapsed,
            "month_elapsed_pct": elapsed_pct,
            "consumption_mtd": _d(mtd),
            "savings_mtd": _d(savings_mtd),
            "floor": floor,
            # Fixed obligations still to land this month. Room is only
            # meaningful net of these: early in the month almost none have
            # posted, so floor - MTD alone reads as far more headroom than
            # actually exists.
            "remaining_committed": remaining_committed,
            "remaining_committed_items": committed,
            "fixed_expected_total": fixed_expected_total,
            "fixed_posted": fixed_posted,
            "variable_mtd": variable_mtd,
            "obligations_excluded_from_floor": excluded,
            "basis": (
                f"Consumption only; {SAVINGS_CATEGORY!r} is excluded and reported "
                "separately. Room is net of fixed obligations that have not yet "
                "posted, and pace is measured on VARIABLE spend only — pacing on "
                "the total makes the 1st-of-month mortgage look like an overspend "
                "spike. Debt-service legs that post as transfers never touch the "
                "consumption floor and are listed under "
                "obligations_excluded_from_floor."
            ),
            "presentation_note": (
                "Present room as '$X after $Y of upcoming fixed bills', never the "
                "naive floor-minus-spend figure."
            ),
        }

        if floor is None:
            for key in (
                "floor_to_date",
                "pace_delta",
                "room_this_month",
                "variable_floor",
                "variable_floor_to_date",
                "variable_pace_delta",
                "required_remaining_pace",
                "recovery_delta",
            ):
                result[key] = None
            result["note"] = "No floor configured (HOMELAB_MCP_FINANCES_FLOOR)."
            # Independent of the floor, so it is still worth reporting.
            result["typical_remaining_pace"] = typical_remaining_pace
        else:
            # Headline: what is genuinely left, after everything already
            # committed for the rest of the month. May be negative.
            result["room_this_month"] = round(floor - _d(mtd) - remaining_committed, 2)
            result["room_this_month_naive"] = round(floor - _d(mtd), 2)

            # The true discretionary floor: what remains once every fixed
            # obligation for the month is paid.
            variable_floor = round(floor - fixed_expected_total, 2)
            variable_to_date = round(variable_floor * days_elapsed / days_in_month, 2)
            result["variable_floor"] = variable_floor
            result["variable_floor_to_date"] = variable_to_date
            # Positive = spending variable faster than the floor allows.
            result["variable_pace_delta"] = round(variable_mtd - variable_to_date, 2)
            result["pace"] = "ahead" if variable_mtd > variable_to_date else "on_or_under"

            # ── recovery math ────────────────────────────────────────
            # PULSE.md's tone rule: an over-pace week gets a number to aim at,
            # never a bare verdict. Negative values are reported, not clamped —
            # a negative required pace is the honest statement that the floor
            # is already spent, and hiding it would be the verdict this is
            # meant to replace.
            #
            # On the last day days_remaining is 0; the divisor floors at 1, so
            # the figure reads "what would have to fit in today" instead of
            # blowing up.
            remaining_days = max(days_in_month - days_elapsed, 1)
            required = round(float(result["room_this_month"]) / remaining_days, 2)
            result["required_remaining_pace"] = required
            result["typical_remaining_pace"] = typical_remaining_pace
            result["recovery_delta"] = (
                round(typical_remaining_pace - required, 2)
                if typical_remaining_pace is not None
                else None
            )
            result["recovery_basis"] = (
                "required_remaining_pace is dollars/day of variable spend that "
                "lands the month exactly on floor. typical_remaining_pace is the "
                "trailing 6-month average daily variable spend on the same basis. "
                "recovery_delta positive = must run tighter than typical by that "
                "much per day; negative = cushion. Divisor floors at 1 day so the "
                "last day of the month is defined."
            )
            result["trailing_months_used"] = len(trailing_dailies)

            # Kept for continuity; measured on total consumption, so it spikes
            # on the 1st. variable_pace_delta is the one to quote.
            floor_to_date = round(floor * days_elapsed / days_in_month, 2)
            result["floor_to_date"] = floor_to_date
            result["pace_delta"] = round(_d(mtd) - floor_to_date, 2)

        if category:
            months = sorted(per_month_cat)
            avg = round(sum(per_month_cat.values()) / 100.0 / len(months), 2) if months else None
            result["category"] = {
                "name": category,
                "mtd": _d(cat_mtd),
                "trailing_months": len(months),
                "trailing_avg": avg,
                "delta_vs_trailing_avg": round(_d(cat_mtd) - avg, 2) if avg is not None else None,
            }
        return result

    # ── 13. reconcile ────────────────────────────────────────────────
    @mcp.tool(
        annotations=_RO,
        name="finances_reconcile",
        description=(
            "Compare Actual's register against the BANK'S OWN reported balance, "
            "per account. This is what catches a register that has drifted from "
            "reality while every transaction reads as cleared — the Jan-Apr 2026 "
            "fault, where the balance was wrong for months and nothing looked "
            "amiss. Each account is classified exact / settlement_window / "
            "structural / market_movement / manual, with the drift and the "
            "account's recent activity so the call is checkable. Detection only; "
            "it changes nothing. If the bank-balance feed is unavailable it "
            "degrades to a cleared-vs-register comparison and says so, rather "
            "than reporting a comparison it did not make."
        ),
    )
    async def reconcile() -> dict[str, Any]:
        try:
            cfg = _config()
            rcfg = cfg.get("reconcile") or {}
            window_days = int(rcfg.get("settlement_window_days", 7))
            alert_threshold = float(rcfg.get("pending_alert_threshold", 25000.0))
            market_valued = set(rcfg.get("market_valued_accounts") or [])
            pending_inclusive = set(rcfg.get("pending_inclusive_accounts") or [])
            now = datetime.now(UTC)
            today = date.today()
            accounts = await _accounts()
            recent = await _transactions(today - timedelta(days=window_days), today)
        except ToolError as err:
            return err.payload()

        # Bank balances are best-effort: their absence downgrades the tool, it
        # does not fail it.
        bank_by_id: dict[str, dict[str, Any]] = {}
        degraded_reason: str | None = None
        try:
            payload = await _get("/simplefin-balances")
            for row in (payload or {}).get("accounts") or []:
                if row.get("simplefin_id"):
                    bank_by_id[str(row["simplefin_id"])] = row
        except ToolError as err:
            degraded_reason = err.message

        # Recent activity sizes the settlement window. Opening balances are
        # excluded: a newly-linked account's setup row is the size of its whole
        # balance, which would swallow any genuine structural drift on it.
        gross: dict[str, float] = defaultdict(float)
        for t in recent:
            if t.get("account_id") and not _is_account_setup(t):
                gross[t["account_id"]] += abs(_d(t["amount_cents"]))

        rows: list[dict[str, Any]] = []
        for a in accounts:
            if a["closed"]:
                continue
            register = _d(a["balance_cents"])
            cleared = _d(a.get("cleared_balance_cents", 0))
            activity = round(gross.get(a["id"], 0.0), 2)
            bank_row = bank_by_id.get(str(a.get("simplefin_id") or ""))
            bank = bank_row.get("balance") if bank_row else None

            account_row: dict[str, Any] = {
                "account": a["name"],
                "register_balance": register,
                "cleared_balance": cleared,
                "bank_reported_balance": bank,
                "uncleared": round(register - cleared, 2),
                "recent_activity_gross": activity,
                "settlement_window_days": window_days,
                "bank_sync_status": a.get("bank_sync_status"),
            }
            if bank_row:
                account_row["bank_balance_date"] = bank_row.get("balance_date")
                account_row["bank_available_balance"] = bank_row.get("available_balance")

            if a.get("last_sync") is None and bank is None:
                # Hand-maintained: there is no bank to disagree with.
                account_row["drift"] = None
                account_row["classification"] = "manual"
            elif bank is None:
                account_row["drift"] = None
                account_row["classification"] = "no_bank_balance"
                account_row["note"] = (
                    degraded_reason
                    or "No bank-reported balance for this account; cannot verify the register."
                )
            else:
                drift = round(register - bank, 2)
                account_row["drift"] = drift
                if a["name"] in market_valued:
                    # Live market value vs a register that only moves on a
                    # transaction. Drift here is valuation, not error.
                    account_row["classification"] = "market_movement"
                    account_row["note"] = (
                        "Market-valued account: the bank reports live value while the "
                        "register moves only when a transaction posts."
                    )
                elif drift == 0:
                    account_row["classification"] = "exact"
                elif abs(drift) <= activity:
                    account_row["classification"] = "settlement_window"
                    if a["name"] in pending_inclusive:
                        account_row["pending_inclusive"] = True
                        account_row["note"] = (
                            "This feed reports a PENDING-INCLUSIVE balance while its "
                            "transaction feed is cleared-only, so some drift is expected "
                            "by construction."
                        )
                else:
                    account_row["classification"] = "structural"
                    account_row["note"] = (
                        "Drift exceeds this account's recent activity, so it is not "
                        "settlement float. This is the anchor-error shape: verify the "
                        "register against the bank."
                    )
                # A pending-inclusive flag explains expected drift; it does not
                # license unlimited drift.
                if (
                    a["name"] in pending_inclusive
                    and abs(drift) > alert_threshold
                    and account_row["classification"] != "structural"
                ):
                    account_row["classification"] = "structural"
                    account_row["note"] = (
                        f"Drift exceeds the {alert_threshold:,.0f} alert threshold. Pending "
                        "items do not explain a gap this large."
                    )
            rows.append(account_row)

        order = {
            "structural": 0,
            "no_bank_balance": 1,
            "settlement_window": 2,
            "market_movement": 3,
            "exact": 4,
            "manual": 5,
        }
        rows.sort(
            key=lambda r: (
                order.get(str(r["classification"]), 9),
                -abs(float(r["drift"] or 0)),
            )
        )
        result: dict[str, Any] = {
            "as_of": now.date().isoformat(),
            "mode": "cleared_only" if degraded_reason else "bank_verified",
            "accounts": rows,
            "needs_attention": [
                r["account"]
                for r in rows
                if r["classification"] in ("structural", "no_bank_balance")
            ],
        }
        if degraded_reason:
            result["limitation"] = (
                "The bank-reported balance feed was unavailable, so this run "
                f"compared register against cleared only ({degraded_reason}). In "
                "that mode a register that drifted from reality while fully "
                "cleared is NOT detectable — the Jan-Apr 2026 fault would be "
                "invisible. Re-run once the sidecar can reach it."
            )
        return result

    # ── 14. subscriptions ────────────────────────────────────────────
    @mcp.tool(
        annotations=_RO,
        name="finances_subscriptions",
        description=(
            "Find recurring merchants by scanning charge history: any payee "
            "billing in at least min_months distinct months. Reports how many "
            "months it appears in, the average and latest charge, its variance, "
            "and — the useful one — when it was FIRST seen, which surfaces "
            "subscription creep that a monthly total hides. Charges already "
            "covered by the recurring-obligations config are flagged as known "
            "so the unknown ones stand out."
        ),
    )
    async def subscriptions(
        min_months: Annotated[int, Field(ge=2, le=12)] = 3,
        lookback_months: Annotated[int, Field(ge=3, le=24)] = 12,
    ) -> dict[str, Any]:
        try:
            cfg = _config()
            today = date.today()
            categories = await _categories()
            income_names = _income_category_names(categories)
            rows = await _transactions(_shift_month(today, lookback_months - 1), today)
        except ToolError as err:
            return err.payload()

        known = [
            str(n).lower()
            for item in ((cfg.get("recurring") or {}).get("items") or [])
            for n in (item.get("match_any") or [])
        ]
        buckets: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for t in rows:
            if not _is_spend(t, income_names) or _is_account_setup(t):
                continue
            payee = (t.get("payee_name") or "").strip()
            if payee:
                buckets[payee].append(t)

        found: list[dict[str, Any]] = []
        for payee, items in buckets.items():
            months = sorted({t["date"][:7] for t in items})
            if len(months) < min_months:
                continue
            amounts = [_d(-t["amount_cents"]) for t in items]
            avg = round(sum(amounts) / len(amounts), 2)
            spread = round(max(amounts) - min(amounts), 2)
            hay = payee.lower()
            found.append(
                {
                    "payee": payee,
                    "months_present": len(months),
                    "first_seen": min(t["date"] for t in items),
                    "last_charge_date": max(t["date"] for t in items),
                    "last_charge_amount": _d(-max(items, key=lambda t: t["date"])["amount_cents"]),
                    "average_charge": avg,
                    "amount_spread": spread,
                    # Flat amount every month is the signature of a subscription;
                    # a wide spread is more likely an ordinary recurring merchant.
                    "fixed_amount": spread <= max(1.0, avg * 0.02),
                    "annualized": round(avg * 12, 2),
                    "known_obligation": any(n in hay for n in known),
                }
            )
        found.sort(key=lambda r: (-int(r["months_present"]), -float(r["annualized"])))
        return {
            "lookback_months": lookback_months,
            "min_months": min_months,
            "returned": len(found),
            "total": len(found),
            "truncated": False,
            "subscriptions": found,
            "unknown_annualized_total": round(
                sum(float(r["annualized"]) for r in found if not r["known_obligation"]), 2
            ),
        }

    # ── 15. net worth ────────────────────────────────────────────────
    @mcp.tool(
        annotations=_RO,
        name="finances_net_worth",
        description=(
            "Full balance-sheet rollup across every open account, on-budget and "
            "off: cash, investments, property and all debt, netted. Also "
            "reports the investable total (liquid + retirement, excluding the "
            "house), home equity, and employer-stock concentration — "
            "employer-tied assets as a share of investable, with components "
            "itemized. The 401(k) is a target-date fund and is explicitly NOT "
            "counted as employer stock. Concentration reports 'unknown' rather "
            "than a low number when the hand-maintained brokerage figure is "
            "unset. Facts only; whether a concentration is acceptable is a "
            "judgment for the household and its advisors. Home equity is DISPLAY-ONLY per PLAN.md's "
            "guardrail — it feeds the net-worth view and the HELOC scoreboard "
            "and must never enter an affordability or spending decision, "
            "because paper equity treated as spendable is how HELOCs happen. "
            "The house valuation is the one hand-maintained figure, updated "
            "quarterly."
        ),
    )
    async def net_worth() -> dict[str, Any]:
        try:
            cfg = _config()
            debt_cfg = {k: v for k, v in (cfg.get("debts") or {}).items() if not k.startswith("_")}
            accounts = await _accounts()
        except ToolError as err:
            return err.payload()

        cash_names = set((cfg.get("buffer") or {}).get("cash_accounts") or [])
        assets: list[dict[str, Any]] = []
        debts: list[dict[str, Any]] = []
        for a in accounts:
            if a["closed"]:
                continue
            bal = _d(a["balance_cents"])
            row = {"account": a["name"], "balance": bal, "offbudget": bool(a["offbudget"])}
            (debts if bal < 0 else assets).append(row)

        def _is(name: str, *needles: str) -> bool:
            low = name.lower()
            return any(n in low for n in needles)

        house = next((a for a in assets if a["account"] == "House"), None)
        property_total = round(house["balance"], 2) if house else 0.0
        cash_total = round(sum(a["balance"] for a in assets if a["account"] in cash_names), 2)
        investable = round(
            sum(
                a["balance"]
                for a in assets
                if a["account"] != "House" and a["account"] not in cash_names
            ),
            2,
        )
        other_liquid = round(
            sum(a["balance"] for a in assets) - property_total - cash_total - investable, 2
        )
        asset_total = round(sum(a["balance"] for a in assets), 2)
        debt_total = round(sum(d["balance"] for d in debts), 2)

        mortgage = next((d for d in debts if _is(d["account"], "mortgage")), None)
        heloc = next((d for d in debts if _is(d["account"], "heloc")), None)
        equity = None
        if house and mortgage:
            equity = round(
                house["balance"] + mortgage["balance"] + (heloc["balance"] if heloc else 0.0), 2
            )

        # ── employer concentration ────────────────────────────────────
        # Facts only. This reports the figure a fiduciary planner opens with;
        # whether it is acceptable is a judgment for humans, not for a tool.
        ccfg = cfg.get("concentration") or {}
        tied_names = [n for n in (ccfg.get("employer_tied_accounts") or [])]
        tied_components: list[dict[str, Any]] = []
        for name in tied_names:
            acct = next((a for a in assets if a["account"] == name), None)
            if acct is not None:
                tied_components.append(
                    {"account": name, "value": acct["balance"], "source": "actual"}
                )
        raw_msft = ccfg.get("msft_shares_in_brokerage")
        msft_value = float(raw_msft) if raw_msft is not None else None
        if msft_value is not None:
            tied_components.append(
                {
                    "account": ccfg.get("msft_shares_source_account", "Fidelity Brokerage"),
                    "value": round(msft_value, 2),
                    "source": "config",
                    "note": (
                        "Vested RSUs still held in employer stock. Feeds carry the "
                        "account balance but not its positions, so this is "
                        "maintained by hand and refreshed at monthly reviews."
                    ),
                }
            )
        tied_total = round(sum(float(c["value"]) for c in tied_components), 2)
        concentration_pct: float | None = None
        if msft_value is not None and investable:
            concentration_pct = round(tied_total / investable * 100.0, 1)

        concentration: dict[str, Any] = {
            "employer_tied_assets": tied_total,
            "investable_total": investable,
            "concentration_pct": concentration_pct,
            "components": tied_components,
            # A separate fact from asset concentration, and deliberately
            # surfaced alongside it: salary, bonuses and every future RSU vest
            # come from the same employer, so the income and the asset are
            # exposed to one company at once.
            "single_employer_income": bool(ccfg.get("single_employer_income")),
            "excluded": [
                {
                    "account": "Microsoft 401k",
                    "reason": (
                        "Invested in a target-date fund, not employer stock. It is "
                        "the largest single account, so counting it would badly "
                        "overstate concentration."
                    ),
                }
            ],
        }
        if concentration_pct is None:
            concentration["concentration_pct_status"] = "unknown"
            concentration["unknown_reason"] = (
                "msft_shares_in_brokerage is not set in config, so the employer-stock "
                "value held in the brokerage is unknown. The ESPP figure above is "
                "real but partial — treat the percentage as unmeasured, not as low."
            )
        else:
            concentration["concentration_pct_status"] = "measured"

        return {
            "as_of": date.today().isoformat(),
            "net_worth": round(asset_total + debt_total, 2),
            "concentration": concentration,
            "assets_total": asset_total,
            "debts_total": debt_total,
            "cash": cash_total,
            "investable": investable,
            "other_liquid": other_liquid,
            "property": property_total,
            "assets": sorted(assets, key=lambda r: -r["balance"]),
            "debts": sorted(debts, key=lambda r: r["balance"]),
            "unclassified_debts": [d["account"] for d in debts if d["account"] not in debt_cfg],
            "home_equity": equity,
            "home_equity_note": (
                "DISPLAY ONLY. Per PLAN.md this never enters an affordability or "
                "spending decision; paper equity treated as spendable is how "
                "HELOCs happen. The house valuation is hand-maintained, updated "
                "quarterly at review."
            ),
        }

    # ── 16. payoff projection ────────────────────────────────────────
    @mcp.tool(
        annotations=_RO,
        name="finances_payoff_projection",
        description=(
            "Amortize a debt to zero from its LIVE balance and its configured "
            "rate: months to payoff, the date, and total interest — with and "
            "without extra monthly payments and one-off lump sums. Always "
            "returns the minimum-only baseline alongside, so the value of "
            "accelerating is explicit. Deterministic month-by-month "
            "arithmetic, not a formula approximation, so lumps land where you "
            "put them. Interest-only debts never amortize on the minimum "
            "alone; that is reported as 'never' rather than a huge number."
        ),
    )
    async def payoff_projection(
        debt_name: Annotated[
            str, Field(min_length=1, description="Account name, e.g. 'Spectra HELOC'.")
        ],
        extra_monthly: Annotated[float, Field(ge=0)] = 0.0,
        lumps: Annotated[
            list[dict[str, Any]] | None,
            Field(description="One-off payments: [{month: 'YYYY-MM', amount: 5000}]."),
        ] = None,
        max_months: Annotated[
            int, Field(ge=12, le=600, description="Give up after this many months.")
        ] = 600,
    ) -> dict[str, Any]:
        try:
            cfg = _config()
            debt_cfg = {k: v for k, v in (cfg.get("debts") or {}).items() if not k.startswith("_")}
            accounts = await _accounts()
        except ToolError as err:
            return err.payload()

        by_name = {a["name"]: a for a in accounts if not a["closed"]}
        matches = [n for n in by_name if debt_name.lower() in n.lower()]
        if len(matches) != 1:
            return ToolError(
                "finances_debt_ambiguous" if matches else "finances_debt_not_found",
                f"{'Several' if matches else 'No'} accounts match {debt_name!r}"
                + (f": {', '.join(matches)}." if matches else "."),
                "Use the exact account name from finances_debt_status.",
            ).payload()
        name = matches[0]
        balance = -_d(by_name[name]["balance_cents"])
        if balance <= 0:
            return {"debt": name, "balance": 0.0, "note": "Nothing outstanding."}

        meta = debt_cfg.get(name) or {}
        rate = meta.get("rate")
        if rate is None:
            return ToolError(
                "finances_rate_unknown",
                f"No rate configured for {name!r}.",
                "Add it to the config's `debts` section; projecting without a "
                "rate would invent the answer.",
            ).payload()
        payment = meta.get("scheduled_payment")
        if payment is None:
            return ToolError(
                "finances_payment_unknown",
                f"No scheduled_payment configured for {name!r}.",
                "Add it to the config's `debts` section.",
            ).payload()

        lump_by_month: dict[str, float] = defaultdict(float)
        for lump in lumps or []:
            try:
                lump_by_month[str(lump["month"])] += float(lump["amount"])
            except (KeyError, TypeError, ValueError):
                return ToolError(
                    "finances_bad_lump",
                    "Each lump needs {month: 'YYYY-MM', amount: <number>}.",
                    "",
                ).payload()

        monthly_rate = float(rate) / 100.0 / 12.0
        start = date.today()

        def simulate(extra: float, use_lumps: bool) -> dict[str, Any]:
            bal = balance
            interest = 0.0
            cursor = start
            for month in range(1, max_months + 1):
                accrued = round(bal * monthly_rate, 2)
                interest += accrued
                bal = round(bal + accrued, 2)
                pay = float(payment) + extra
                if use_lumps:
                    pay += lump_by_month.get(cursor.strftime("%Y-%m"), 0.0)
                # A payment that doesn't cover interest never retires anything.
                if pay <= accrued and not (use_lumps and lump_by_month):
                    return {
                        "months": None,
                        "payoff_date": None,
                        "total_interest": None,
                        "note": (
                            "The scheduled payment does not exceed monthly interest, "
                            "so this debt never amortizes on it alone."
                        ),
                    }
                bal = round(bal - pay, 2)
                if bal <= 0:
                    return {
                        "months": month,
                        "payoff_date": cursor.strftime("%Y-%m"),
                        "total_interest": round(interest + bal, 2)
                        if bal < 0
                        else round(interest, 2),
                        "note": None,
                    }
                cursor = _shift_month(cursor, -1)
            return {
                "months": None,
                "payoff_date": None,
                "total_interest": None,
                "note": f"Not retired within {max_months} months.",
            }

        baseline = simulate(0.0, use_lumps=False)
        plan = simulate(float(extra_monthly), use_lumps=True)
        saved = (
            round(float(baseline["total_interest"]) - float(plan["total_interest"]), 2)
            if baseline["total_interest"] is not None and plan["total_interest"] is not None
            else None
        )
        return {
            "debt": name,
            "balance": balance,
            "rate": float(rate),
            "is_variable": bool(meta.get("is_variable")),
            "scheduled_payment": float(payment),
            "extra_monthly": float(extra_monthly),
            "lumps": [{"month": m, "amount": a} for m, a in sorted(lump_by_month.items())],
            "projection": plan,
            "minimum_only_baseline": baseline,
            "interest_saved_vs_baseline": saved,
            "assumptions": (
                "Interest accrues monthly on the outstanding balance at the "
                "configured rate, held constant. A variable rate will not stay "
                "where it is, so treat a variable-rate projection as a scenario."
            ),
        }
