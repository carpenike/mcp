"""Amazon tools — what was actually in the box, read-only.

The ledger says `Amazon.com  -$84.31`. These tools say it was a furnace
filter. That is the whole job.

**Nothing here fetches from Amazon.** The credential, the login and the HTML
parsing live in `lading` (github:carpenike/lading), a separate service that
syncs once a day into its own Postgres. This category reads that store
through a role with `readonly` membership and answers in milliseconds. The
split matters: lading's credential can place orders and change shipping
addresses, and a model that retries a failing tool is exactly how an Amazon
account gets challenge-locked. `openWorldHint=False` below is truthful
*because* of that split.

**These tools never categorize.** They report facts about purchases. Deciding
which budget category a purchase belongs to is `finances_categorize`'s job,
and the two are deliberately not wired together — this category knows nothing
about Actual, and the caller hands charges over in a batch.

Tool name convention: `amazon_<verb>_<object>`. See AGENTS.md.
"""

from __future__ import annotations

import logging
import re
from datetime import UTC, date, datetime, timedelta
from typing import TYPE_CHECKING, Annotated, Any

from mcp.types import ToolAnnotations
from pydantic import BaseModel, ConfigDict, Field

from homelab_mcp.tools._http import ToolError
from homelab_mcp.tools._pg import Reader, envelope, load_zone, render_row, store_error

if TYPE_CHECKING:
    from mcp.server.fastmcp import FastMCP

    from homelab_mcp.config import Settings

log = logging.getLogger(__name__)

_STORE = "lading"
_STORE_ENV = "HOMELAB_MCP_AMAZON_DATABASE_URL"

# Every tool here is a read against a local database: read-only, idempotent,
# and closed-world *because the scraping happens in another service*. That is
# the concrete payoff of the split — see the module docstring.
_RO = ToolAnnotations(
    readOnlyHint=True, destructiveHint=False, idempotentHint=True, openWorldHint=False
)

# Hard cap on any list response.
MAX_ROWS = 200
# Cap on line items returned across a whole match batch. Fifty orders of line
# items would flood the context and bury the answer.
MAX_BATCH_ITEMS = 300
MAX_CHARGES = 50

# Amazon order numbers are digits and hyphens. Validated at the boundary per
# AGENTS.md rule 3 even though it reaches SQL as a bound parameter — a strict
# shape check is cheap and keeps a malformed id from becoming a confusing
# empty result.
ORDER_NUMBER = re.compile(r"[0-9-]{10,25}")

# Orders are keyed by (account, order_number) everywhere, because the account
# is part of the primary key in lading's schema — order numbers are believed
# globally unique across Amazon, but the store does not bet on it.
OrderKey = tuple[str, str]

INSTRUCTIONS = """
Amazon purchase history for the household, synced once a day from
amazon.com by a separate service. Nothing here is live: every response
carries `data_as_of` and `stale`.

Call amazon_get_sync_status before telling anyone a charge has no order
behind it. An empty match and a scraper that broke a week ago look
identical otherwise.

AN AMAZON CHARGE IS NOT AN AMAZON ORDER. One order splits across shipments
into several charges. Prime, AWS, Kindle and Audible post as Amazon charges
with nothing shipped behind them. A `none` match is common and usually not
an error.

`confidence` is `ambiguous` when several orders fit. Present the options;
do not pick one.

Purchases paid from Amazon BALANCE (`funding: "balance"`) never posted to a
card, so they are invisible to the bank feed and to Actual. That balance is
usually credit from a return — Amazon refunds returns to your balance rather
than the paying card — but it can also be a received gift card, and this
data CANNOT tell the two apart. Say "Amazon balance", never "return credit".
The ledger consequence is real: a returned item's expense never leaves
Actual, and whatever the credit later bought never enters it.

Whole Foods orders arrive through the same feed and are groceries.

These tools describe purchases. They never categorize — pair them with
finances_categorize, which is the tool that decides.
""".strip()


def _d(cents: int | None) -> float | None:
    """Integer cents to dollars, rounded to the cent.

    Mirrors `finances._d`. Cents are authoritative and stay on the wire
    everywhere inside this module; dollars are for the reader. Every amount
    comparison happens in integers — see `match_charge`.
    """
    return None if cents is None else round(cents / 100.0, 2)


def to_cents(dollars: float) -> int:
    """Dollars to integer cents, half-up, without trusting binary floats."""
    return int(round(dollars * 100))


class Charge(BaseModel):
    """One bank charge to explain.

    `extra="forbid"` for the same reason `finances.Assignment` uses it: a
    caller that tries to pass a category, a payee or an instruction has
    misunderstood what this tool does, and should get a validation error
    rather than have the field silently ignored.
    """

    model_config = ConfigDict(extra="forbid")

    ref: str = Field(
        min_length=1, description="Your id, echoed back. Use the Actual transaction id."
    )
    date: str = Field(description="Bank posting date, 'YYYY-MM-DD'.")
    amount: float = Field(
        description="Dollars, signed as Actual signs it: spend negative, refund positive."
    )
    account: str | None = Field(
        default=None, description="Actual account name, for card last-4 disambiguation."
    )


def parse_day(value: str, *, field: str) -> date:
    """Parse a YYYY-MM-DD date, or raise the shared error contract."""
    try:
        return date.fromisoformat(value.strip())
    except (ValueError, AttributeError):
        raise ToolError(
            "bad_date", f"{field} must be YYYY-MM-DD, got {value!r}.", "Example: 2026-08-04."
        ) from None


def funding_of(payment_method: str | None, last_4: str | None) -> str:
    """Whether a charge came off a card or off Amazon balance.

    Balance-funded purchases have no card and therefore no bank counterpart —
    they are the ones the ledger silently misses. `last_4` is the reliable
    signal; the payment-method string is a display label that has changed
    shape before.
    """
    if last_4:
        return "card"
    if payment_method and "gift card" in payment_method.lower():
        return "balance"
    return "unknown"


def match_charge(
    charge_cents: int,
    charge_day: date,
    candidates: list[dict[str, Any]],
    *,
    expected_last_4: str | None,
) -> tuple[str, list[dict[str, Any]]]:
    """Pick among same-amount candidates. Returns (confidence, chosen).

    Pure: no database, no clock. The SQL has already narrowed to rows whose
    amount matches EXACTLY and whose date is inside the window; this decides
    how much to believe the result.

    The last-4 filter is applied only when it can actually be applied — the
    caller named an account, that account has a configured card, and the
    candidate row knows its card. Any of those missing lowers confidence
    instead of dropping candidates: never discard a row over a comparison you
    could not make.
    """
    if not candidates:
        return "none", []

    usable = [c for c in candidates if c.get("payment_method_last_4")]
    if expected_last_4 and usable:
        narrowed = [c for c in usable if c["payment_method_last_4"] == expected_last_4]
        if narrowed:
            if len({c.get("order_number") for c in narrowed}) == 1:
                return "exact", narrowed
            return "ambiguous", narrowed
        # The account's card is known and NO candidate matches it. That is
        # evidence against every candidate, not for one of them.
        return "ambiguous", candidates

    orders = {c.get("order_number") for c in candidates}
    if len(orders) == 1:
        # One order, whether that is one candidate or several charges against
        # the same order. Not `exact`: the card was never verified.
        return "probable", candidates
    return "ambiguous", candidates


def none_reason(
    charge_day: date,
    covered_periods: set[date],
    *,
    today: date,
    stale: bool,
) -> str:
    """Why nothing matched — the distinction the household's money rests on.

    `outside_coverage` and `no_amount_match` mean opposite things and must
    never be collapsed: one is "we have never looked at that month", the other
    is "we looked and there is genuinely no such charge". Reporting the first
    as the second is a confident lie.
    """
    if charge_day.replace(day=1) not in covered_periods:
        return "outside_coverage"
    if stale and (today - charge_day).days <= 2:
        return "stale_sync"
    return "no_amount_match"


def register(mcp: FastMCP, settings: Settings) -> None:
    """Register amazon_* tools, if a lading database is configured."""
    dsn = settings.amazon_database_url
    if not dsn:
        log.info("%s unset — amazon tools not registered", _STORE_ENV)
        return

    db = Reader(dsn)
    zone = load_zone(settings.amazon_timezone, label=_STORE)
    last4_by_account = {k.strip().lower(): v for k, v in settings.amazon_account_last4.items()}

    def row(record: Any) -> dict[str, Any]:
        return render_row(record, zone)

    def err(exc: Exception) -> dict[str, Any]:
        return store_error(
            exc, code="lading_unreachable", store=_STORE, env_var=_STORE_ENV
        ).payload()

    # ── helpers ──────────────────────────────────────────────────────

    async def freshness() -> dict[str, Any]:
        """Most recent successful sync, per account and source."""
        rows = await db.fetch(
            "SELECT DISTINCT ON (account, source) account, source, finished_at, status,"
            " records_changed, parsers_pending, error FROM ingest_runs"
            " WHERE finished_at IS NOT NULL"
            " ORDER BY account, source, finished_at DESC"
        )
        ok = [r["finished_at"] for r in rows if r["status"] in ("ok", "partial")]
        latest: datetime | None = max(ok) if ok else None
        age = None if latest is None else (datetime.now(UTC) - latest).total_seconds() / 3600
        return {
            "data_as_of": None if latest is None else latest.astimezone(zone).isoformat(),
            "stale": latest is None or (age or 0) > settings.amazon_stale_after_hours,
            "age_hours": None if age is None else round(age, 1),
            "runs": [row(r) for r in rows],
        }

    async def stamped(payload: dict[str, Any]) -> dict[str, Any]:
        """Attach the freshness marker every response carries."""
        fresh = await freshness()
        payload["data_as_of"] = fresh["data_as_of"]
        payload["stale"] = fresh["stale"]
        return payload

    async def items_for(
        order_numbers: list[OrderKey], budget: int
    ) -> dict[OrderKey, list[dict[str, Any]]]:
        """Line items for (account, order_number) pairs, capped by `budget`."""
        if not order_numbers or budget <= 0:
            return {}
        accounts = [a for a, _ in order_numbers]
        numbers = [n for _, n in order_numbers]
        rows = await db.fetch(
            "SELECT account, order_number, position, title, asin, price_cents, quantity, seller"
            " FROM amazon_items"
            " WHERE (account, order_number) = ANY("
            "   SELECT unnest($1::text[]), unnest($2::text[]))"
            " ORDER BY account, order_number, position LIMIT $3",
            accounts,
            numbers,
            budget,
        )
        out: dict[OrderKey, list[dict[str, Any]]] = {}
        for r in rows:
            out.setdefault((r["account"], r["order_number"]), []).append(
                {
                    "title": r["title"],
                    "asin": r["asin"],
                    "quantity": r["quantity"],
                    "price": _d(r["price_cents"]),
                    "seller": r["seller"],
                }
            )
        return out

    def order_payload(o: dict[str, Any]) -> dict[str, Any]:
        """Render an order row. Surfaces the gift-card line deliberately.

        A fully balance-funded order reports `grand_total = $0.00` — Amazon
        means "nothing was charged to a card", not "this was free". Returning
        the total without `gift_card` beside it makes an $18 order read as
        free, so both always travel together.
        """
        return {
            "order_number": o["order_number"],
            "account": o["account"],
            "order_placed_date": o["order_placed_date"].isoformat()
            if o.get("order_placed_date")
            else None,
            "grand_total": _d(o.get("grand_total_cents")),
            "gift_card": _d(o.get("gift_card_cents")),
            "estimated_tax": _d(o.get("estimated_tax_cents")),
            "shipping_total": _d(o.get("shipping_total_cents")),
            "promotion": _d(o.get("promotion_cents")),
            "coupon_savings": _d(o.get("coupon_savings_cents")),
            "subscription_discount": _d(o.get("subscription_discount_cents")),
            "refund_total": _d(o.get("refund_total_cents")),
            "payment_method": o.get("payment_method"),
            "card_last_4": o.get("payment_method_last_4"),
            "recipient": o.get("recipient"),
            "cancelled": o.get("cancelled"),
            "is_whole_foods": o.get("is_whole_foods"),
            "item_count": o.get("item_count"),
            # False means the per-order detail page has not been fetched yet,
            # so tax, discounts and card last-4 are NULL for that reason and
            # not because the order lacked them. Say so rather than reporting
            # a $0 tax.
            "full_details": o.get("full_details"),
        }

    # ── tools ────────────────────────────────────────────────────────

    @mcp.tool(
        annotations=_RO,
        name="amazon_get_sync_status",
        description=(
            "When the Amazon sync last succeeded, per account and source, and "
            "which calendar months have actually been fetched. CALL THIS "
            "BEFORE telling anyone a charge has no Amazon order behind it — "
            "an empty match and a scraper that broke a week ago are "
            "indistinguishable otherwise."
        ),
    )
    async def get_sync_status() -> dict[str, Any]:
        try:
            fresh = await freshness()
            coverage = await db.fetch(
                "SELECT account, source, min(period) AS covered_from,"
                " max(period) AS covered_to, count(*) AS months"
                " FROM sync_coverage GROUP BY account, source ORDER BY account, source"
            )
        except Exception as exc:  # noqa: BLE001 — never raise to the transport
            return err(exc)
        return {
            "data_as_of": fresh["data_as_of"],
            "stale": fresh["stale"],
            "age_hours": fresh["age_hours"],
            "stale_after_hours": settings.amazon_stale_after_hours,
            "runs": fresh["runs"],
            "coverage": [row(c) for c in coverage],
        }

    @mcp.tool(
        annotations=_RO,
        name="amazon_match_charges",
        description=(
            "Given bank charges from the ledger, find the Amazon orders and "
            "line items behind them. Batch: pull the rows with "
            "finances_transactions(payee_contains='amazon') and hand them "
            "over in ONE call, keyed by your own `ref`. Returns a confidence "
            "per charge (exact / probable / ambiguous / none) and, when "
            "nothing matched, WHY — `outside_coverage` means the month was "
            "never synced and must not be reported as 'no such order'. Never "
            "picks among ambiguous candidates, and never suggests a category."
        ),
    )
    async def match_charges(
        charges: Annotated[
            list[Charge], Field(description="1-50 bank charges to explain.", min_length=1)
        ],
        window_days: Annotated[
            int | None,
            Field(default=None, ge=0, le=10, description="Override the date-window tolerance."),
        ] = None,
    ) -> dict[str, Any]:
        if len(charges) > MAX_CHARGES:
            return ToolError(
                "too_many_charges",
                f"{len(charges)} charges; the cap is {MAX_CHARGES}.",
                "Split the batch.",
            ).payload()
        window = settings.amazon_match_window_days if window_days is None else window_days
        today = datetime.now(UTC).astimezone(zone).date()

        try:
            fresh = await freshness()
            cov_rows = await db.fetch(
                "SELECT DISTINCT period FROM sync_coverage WHERE source = 'transactions'"
            )
            covered = {r["period"] for r in cov_rows}

            results: list[dict[str, Any]] = []
            wanted: list[tuple[str, str]] = []
            for ch in charges:
                day = parse_day(ch.date, field="date")
                cents = to_cents(ch.amount)
                # Amount is compared EXACTLY, and always in integers. A near
                # miss is a different purchase, not the same one.
                cands = await db.fetch(
                    "SELECT t.id, t.account, t.completed_date, t.amount_cents, t.is_refund,"
                    " t.payment_method, t.payment_method_last_4, t.seller, t.order_number"
                    " FROM amazon_transactions t"
                    " WHERE t.amount_cents = $1"
                    "   AND t.is_refund = $2"
                    "   AND t.completed_date BETWEEN $3 AND $4"
                    " ORDER BY t.completed_date, t.account, t.id",
                    cents,
                    cents > 0,
                    day - timedelta(days=window),
                    # Bank posting lags Amazon's completion; it rarely leads,
                    # but clock and timezone skew make a one-day lead possible.
                    day + timedelta(days=1),
                )
                expected = last4_by_account.get((ch.account or "").strip().lower())
                confidence, chosen = match_charge(
                    cents, day, [dict(c) for c in cands], expected_last_4=expected
                )
                entry: dict[str, Any] = {"ref": ch.ref, "confidence": confidence}
                if confidence == "none":
                    entry["reason"] = none_reason(
                        day, covered, today=today, stale=bool(fresh["stale"])
                    )
                    entry["candidates"] = []
                else:
                    entry["candidates"] = [
                        {
                            "account": c["account"],
                            "completed_date": c["completed_date"].isoformat(),
                            "amount": _d(c["amount_cents"]),
                            "is_refund": c["is_refund"],
                            "payment_method": c["payment_method"],
                            "card_last_4": c["payment_method_last_4"],
                            "funding": funding_of(c["payment_method"], c["payment_method_last_4"]),
                            "seller": c["seller"],
                            "order_number": c["order_number"],
                        }
                        for c in chosen
                    ]
                    for c in chosen:
                        if c["order_number"]:
                            wanted.append((c["account"], c["order_number"]))
                results.append(entry)

            orders: dict[OrderKey, dict[str, Any]] = {}
            if wanted:
                accounts = [a for a, _ in wanted]
                numbers = [n for _, n in wanted]
                order_rows = await db.fetch(
                    "SELECT * FROM amazon_orders WHERE (account, order_number) = ANY("
                    " SELECT unnest($1::text[]), unnest($2::text[]))",
                    accounts,
                    numbers,
                )
                orders = {(o["account"], o["order_number"]): dict(o) for o in order_rows}
                items = await items_for(list(dict.fromkeys(wanted)), MAX_BATCH_ITEMS)
                for o_key, o in orders.items():
                    o["_items"] = items.get(o_key, [])
        except ToolError as exc:
            return exc.payload()
        except Exception as exc:  # noqa: BLE001 — never raise to the transport
            return err(exc)

        item_count = 0
        for entry in results:
            for cand in entry.get("candidates", []):
                key = (cand["account"], cand["order_number"])
                order_row = orders.get(key)
                if order_row is None:
                    # A charge with no order behind it: a gift-card reload, a
                    # digital purchase, or an order outside the synced window.
                    cand["order"] = None
                    continue
                rendered = order_payload(order_row)
                rendered["items"] = order_row.get("_items", [])
                item_count += len(rendered["items"])
                cand["order"] = rendered

        matched = sum(1 for r in results if r["confidence"] != "none")
        out = envelope(results, len(results), "matches")
        out["matched"] = matched
        out["window_days"] = window
        out["item_cap"] = MAX_BATCH_ITEMS
        out["items_truncated"] = item_count >= MAX_BATCH_ITEMS
        return await stamped(out)

    @mcp.tool(
        annotations=_RO,
        name="amazon_get_order",
        description=(
            "One order in full: line items, the totals breakdown, shipments "
            "and recipient. A $0.00 grand total with a gift_card amount means "
            "it was paid from Amazon balance, not that it was free. When "
            "full_details is false the per-order page has not been fetched "
            "yet, so tax and card last-4 are null for that reason alone."
        ),
    )
    async def get_order(
        order_number: Annotated[
            str, Field(description="Amazon order number, e.g. 111-2223334-4445556")
        ],
        account: Annotated[
            str | None, Field(description="Which Amazon account. Omit to search all.")
        ] = None,
    ) -> dict[str, Any]:
        # Validated at the boundary per AGENTS.md rule 3, even though it goes
        # into a parameterized query.
        cleaned = order_number.strip()
        if not ORDER_NUMBER.fullmatch(cleaned):
            return ToolError(
                "bad_order_number",
                f"{order_number!r} is not an Amazon order number.",
                "Expected digits and hyphens, e.g. 111-2223334-4445556.",
            ).payload()
        try:
            rows = await db.fetch(
                "SELECT * FROM amazon_orders WHERE order_number = $1"
                " AND ($2::text IS NULL OR account = $2)",
                cleaned,
                account,
            )
            if not rows:
                return await stamped(
                    {
                        "order_number": cleaned,
                        "found": False,
                        "hint": (
                            "Not in the store. Check amazon_get_sync_status — the month may "
                            "never have been synced."
                        ),
                    }
                )
            found = [dict(r) for r in rows]
            keys = [(o["account"], o["order_number"]) for o in found]
            items = await items_for(keys, MAX_ROWS)
            ship_rows = await db.fetch(
                "SELECT account, order_number, position, delivery_status, tracking_link"
                " FROM amazon_shipments WHERE (account, order_number) = ANY("
                " SELECT unnest($1::text[]), unnest($2::text[])) ORDER BY position",
                [a for a, _ in keys],
                [n for _, n in keys],
            )
        except Exception as exc:  # noqa: BLE001 — never raise to the transport
            return err(exc)

        payloads = []
        for o in found:
            key = (o["account"], o["order_number"])
            p = order_payload(o)
            p["items"] = items.get(key, [])
            p["shipments"] = [
                {"delivery_status": s["delivery_status"], "tracking_link": s["tracking_link"]}
                for s in ship_rows
                if (s["account"], s["order_number"]) == key
            ]
            p["funding"] = funding_of(o.get("payment_method"), o.get("payment_method_last_4"))
            payloads.append(p)
        return await stamped({"found": True, "orders": payloads})

    @mcp.tool(
        annotations=_RO,
        name="amazon_search_items",
        description=(
            "Full-text search over purchased item titles, newest first — "
            "'when did we last buy furnace filters'. Returns the owning "
            "order's date and number so you can follow up with "
            "amazon_get_order."
        ),
    )
    async def search_items(
        query: Annotated[str, Field(min_length=2, description="Words to search for in titles.")],
        since: Annotated[
            str | None, Field(description="Only orders on/after 'YYYY-MM-DD'.")
        ] = None,
        until: Annotated[
            str | None, Field(description="Only orders on/before 'YYYY-MM-DD'.")
        ] = None,
        limit: Annotated[int, Field(ge=1, le=MAX_ROWS, description="Max items.")] = 25,
    ) -> dict[str, Any]:
        try:
            start = parse_day(since, field="since") if since else None
            end = parse_day(until, field="until") if until else None
            rows = await db.fetch(
                "SELECT i.account, i.order_number, i.title, i.asin, i.price_cents, i.quantity,"
                " i.seller, o.order_placed_date, o.is_whole_foods"
                " FROM amazon_items i"
                " JOIN amazon_orders o"
                "   ON o.account = i.account AND o.order_number = i.order_number"
                " WHERE to_tsvector('english', i.title) @@ plainto_tsquery('english', $1)"
                "   AND ($2::date IS NULL OR o.order_placed_date >= $2)"
                "   AND ($3::date IS NULL OR o.order_placed_date <= $3)"
                " ORDER BY o.order_placed_date DESC, i.order_number, i.position"
                " LIMIT $4",
                query,
                start,
                end,
                limit,
            )
            total = await db.fetchval(
                "SELECT count(*) FROM amazon_items i"
                " JOIN amazon_orders o"
                "   ON o.account = i.account AND o.order_number = i.order_number"
                " WHERE to_tsvector('english', i.title) @@ plainto_tsquery('english', $1)"
                "   AND ($2::date IS NULL OR o.order_placed_date >= $2)"
                "   AND ($3::date IS NULL OR o.order_placed_date <= $3)",
                query,
                start,
                end,
            )
        except ToolError as exc:
            return exc.payload()
        except Exception as exc:  # noqa: BLE001 — never raise to the transport
            return err(exc)
        items = [
            {
                "title": r["title"],
                "asin": r["asin"],
                "quantity": r["quantity"],
                "price": _d(r["price_cents"]),
                "seller": r["seller"],
                "account": r["account"],
                "order_number": r["order_number"],
                "order_placed_date": r["order_placed_date"].isoformat(),
                "is_whole_foods": r["is_whole_foods"],
            }
            for r in rows
        ]
        return await stamped(envelope(items, int(total or 0), "items"))

    @mcp.tool(
        annotations=_RO,
        name="amazon_list_orders",
        description=(
            "Browse orders in a date window — 'what did we order in July' — "
            "with totals, item counts and the first few item titles, without "
            "one call per order. `funding: balance` marks orders paid from "
            "Amazon balance, which never appear in the bank feed at all."
        ),
    )
    async def list_orders(
        date_from: Annotated[str, Field(description="Start date, 'YYYY-MM-DD'.")],
        date_to: Annotated[str, Field(description="End date, 'YYYY-MM-DD'.")],
        account: Annotated[str | None, Field(description="Amazon account. Omit for all.")] = None,
        limit: Annotated[int, Field(ge=1, le=MAX_ROWS, description="Max orders.")] = 50,
    ) -> dict[str, Any]:
        try:
            start = parse_day(date_from, field="date_from")
            end = parse_day(date_to, field="date_to")
            if end < start:
                raise ToolError(
                    "bad_range", "date_to is before date_from.", "Swap the two arguments."
                )
            rows = await db.fetch(
                "SELECT * FROM amazon_orders"
                " WHERE order_placed_date BETWEEN $1 AND $2"
                "   AND ($3::text IS NULL OR account = $3)"
                " ORDER BY order_placed_date DESC, order_number LIMIT $4",
                start,
                end,
                account,
                limit,
            )
            total = await db.fetchval(
                "SELECT count(*) FROM amazon_orders"
                " WHERE order_placed_date BETWEEN $1 AND $2"
                "   AND ($3::text IS NULL OR account = $3)",
                start,
                end,
                account,
            )
            found = [dict(r) for r in rows]
            items = await items_for(
                [(o["account"], o["order_number"]) for o in found], MAX_BATCH_ITEMS
            )
        except ToolError as exc:
            return exc.payload()
        except Exception as exc:  # noqa: BLE001 — never raise to the transport
            return err(exc)

        out = []
        for o in found:
            p = order_payload(o)
            titles = [i["title"] for i in items.get((o["account"], o["order_number"]), [])]
            p["item_titles"] = titles[:3]
            p["more_items"] = max(0, len(titles) - 3)
            p["funding"] = funding_of(o.get("payment_method"), o.get("payment_method_last_4"))
            out.append(p)
        return await stamped(envelope(out, int(total or 0), "orders"))
