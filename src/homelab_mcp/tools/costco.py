"""Costco tools — what was actually in the cart, read-only.

The ledger says `COSTCO WHSE #0330  -$219.18`. These tools say it was a
warehouse run with 29 items, and which ones. That is the whole job.

**Nothing here fetches from costco.com.** The credential, the OAuth refresh and
the GraphQL call live in `lading` (github:carpenike/lading), the same service
that syncs Amazon, into the same Postgres. This category reads that store
through a role with `readonly` membership.

**Why this is a separate category from `amazon_*` and not a merged one.** The
two share their JUDGEMENT — `exact`, `probable`, `ambiguous`, `none`,
`outside_coverage`, `oversubscribed` all come from `_purchases` and mean
exactly the same thing here as there. What they do not share is their
VOCABULARY, and the differences are not cosmetic:

  * An Amazon charge is not an Amazon order; a Costco receipt IS the charge.
  * Amazon authorises at order and captures at ship, so its charges post one
    to three days late. A warehouse purchase settles at the register.
  * Amazon balance funds purchases invisibly to the bank feed. Costco has no
    equivalent.
  * Amazon quantity is usually unknown. Costco reports it on every line.

Folding those into one tool would mean a description that forks on source for
almost every sentence, and a response where half the fields are null depending
on who answered. The Amazon caveats were expensive to learn; diluting them is
how they stop being read.

Tool name convention: `costco_<verb>_<object>`. See AGENTS.md.
"""

from __future__ import annotations

import logging
import re
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Annotated, Any

import asyncpg
from pydantic import Field

from homelab_mcp.tools._http import ToolError
from homelab_mcp.tools._pg import Reader, envelope, load_zone, render_row, store_error
from homelab_mcp.tools._purchases import (
    MAX_CHARGES,
    READ_ONLY,
    Charge,
    flag_oversubscribed,
    match_charge,
    none_reason,
    parse_day,
    to_cents,
)
from homelab_mcp.tools._purchases import dollars as _d

if TYPE_CHECKING:
    from mcp.server.fastmcp import FastMCP

    from homelab_mcp.config import Settings

log = logging.getLogger(__name__)

_STORE = "lading"
_STORE_ENV = "HOMELAB_MCP_LADING_DATABASE_URL (or the older _AMAZON_ name)"
_SOURCE = "costco"

MAX_ROWS = 200
# Cap on line items across a whole match batch. A warehouse run is 20-30 lines,
# so a batch of fifty would bury the answer.
MAX_BATCH_ITEMS = 300

# Costco's own receipt id. 23 characters in observed data, but validated as a
# shape rather than a length: a length check on a third party's identifier buys
# nothing and breaks the day they add a digit. Validated at the boundary per
# AGENTS.md rule 3 even though it reaches SQL as a bound parameter.
BARCODE = re.compile(r"\A[0-9]{8,40}\Z")

# (account, transaction_barcode) — the receipt's primary key in lading.
ReceiptKey = tuple[str, str]

# Every column the candidate payload reads, SINGLE SOURCE OF TRUTH: the SELECT
# is built from this and the tests build their fakes from it.
#
# This is not stylistic. The amazon_* category learned it the hard way — a
# payload field was added without adding it to the query, every real call
# returned a KeyError, and the whole suite stayed green because the
# hand-written fakes carried a column the real query never asked for. Fakes
# richer than the query hide exactly that.
TENDER_COLUMNS = (
    "t.id AS tender_id",
    "t.account",
    "t.transaction_barcode",
    "t.amount_cents",
    "t.card_last_4",
    "t.tender_description",
    "r.transaction_date",
    "r.warehouse_name",
    "r.total_cents",
    "r.total_item_count",
    "r.tender_count",
)

INSTRUCTIONS = """
Costco warehouse receipts for the household, synced once a day from
costco.com by a separate service. Nothing here is live: every response
carries `data_as_of` and `stale`.

Call costco_get_sync_status before telling anyone a charge has no receipt
behind it. An empty match and a sync that broke a week ago look identical
otherwise.

AMAZON PURCHASES ARE NOT IN THIS DATA. Use amazon_match_charges for those;
these two categories read different stores' tables and neither falls back to
the other. A charge sweep that only calls one of them will report the other
retailer's charges as unexplained.

A COSTCO RECEIPT IS THE CHARGE. Unlike Amazon there is no
authorise-then-capture gap and no order/shipment split: a warehouse purchase
settles at the register, so the receipt's own date is the charge's date and
the match window is a day, not three.

Match on the TENDER, not the receipt total. A receipt paid with two cards
produces two bank charges, neither equal to the total. `tender_count` above 1
says that happened.

`exact` means the card last-4 was verified; every Costco receipt carries a
card, so `exact` is the normal result here rather than a lucky one — which is
the opposite of the Amazon side. `probable` means the amount and date fit but
no card could be checked.

RETURNS ARE NOT REPRESENTED. Every receipt observed is a sale. A refund on
the ledger (a positive amount) gets `refund_unsupported` rather than being
matched to a same-amount purchase, which would be a confident lie.

Costco only publishes about two years of history, so older charges are
`outside_coverage` permanently, not temporarily.

These tools describe purchases. They never categorize — pair them with
finances_categorize, which is the tool that decides.
""".strip()


def register(mcp: FastMCP, settings: Settings) -> None:
    """Register costco_* tools, if a lading database is configured."""
    dsn = settings.lading_dsn
    if not dsn:
        log.info("%s unset — costco tools not registered", _STORE_ENV)
        return

    db = Reader(dsn)
    zone = load_zone(settings.lading_zone, label=_STORE)
    last4_by_account = {k.strip().lower(): v for k, v in settings.lading_last4.items()}

    def row(record: Any) -> dict[str, Any]:
        return render_row(record, zone)

    def err(exc: Exception) -> dict[str, Any]:
        # A MISSING TABLE is not an unreachable store, and conflating them
        # sends the reader to the wrong place entirely. This is the expected
        # state during a staggered deploy: homelab-mcp picks up the costco_*
        # category as soon as it ships (Renovate auto-merges its bumps within
        # minutes), while the tables only appear when the host applies a lading
        # new enough to have run migration 0004. Reporting that window as
        # `lading_unreachable` would have someone checking the DSN, the role
        # and the network, none of which are wrong.
        if isinstance(exc, asyncpg.exceptions.UndefinedTableError):
            return ToolError(
                "costco_not_migrated",
                "The lading store has no Costco tables yet.",
                "Deploy lading >= 0.3.0 on the host; its migrations create them"
                " on the next run of lading-migrate.",
            ).payload()
        return store_error(
            exc, code="lading_unreachable", store=_STORE, env_var=_STORE_ENV
        ).payload()

    # ── helpers ──────────────────────────────────────────────────────

    async def freshness() -> dict[str, Any]:
        """Most recent successful Costco sync, per account."""
        rows = await db.fetch(
            "SELECT DISTINCT ON (account) account, source, finished_at, status,"
            " records_changed, parsers_pending, error FROM ingest_runs"
            " WHERE source = $1 AND finished_at IS NOT NULL"
            " ORDER BY account, finished_at DESC",
            _SOURCE,
        )
        ok = [r["finished_at"] for r in rows if r["status"] in ("ok", "partial")]
        latest: datetime | None = max(ok) if ok else None
        age = None if latest is None else (datetime.now(UTC) - latest).total_seconds() / 3600
        return {
            "data_as_of": None if latest is None else latest.astimezone(zone).isoformat(),
            "stale": latest is None or (age or 0) > settings.costco_stale_after_hours,
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
        keys: list[ReceiptKey], budget: int
    ) -> dict[ReceiptKey, list[dict[str, Any]]]:
        """Line items for (account, barcode) pairs, capped by `budget`."""
        if not keys or budget <= 0:
            return {}
        accounts = [a for a, _ in keys]
        barcodes = [b for _, b in keys]
        rows = await db.fetch(
            "SELECT account, transaction_barcode, position, description, item_number,"
            " quantity, amount_cents, unit_price_cents"
            " FROM costco_items"
            " WHERE (account, transaction_barcode) = ANY("
            "   SELECT unnest($1::text[]), unnest($2::text[]))"
            " ORDER BY account, transaction_barcode, position LIMIT $3",
            accounts,
            barcodes,
            budget,
        )
        out: dict[ReceiptKey, list[dict[str, Any]]] = {}
        for r in rows:
            out.setdefault((r["account"], r["transaction_barcode"]), []).append(
                {
                    "description": r["description"],
                    "item_number": r["item_number"],
                    "quantity": r["quantity"],
                    "amount": _d(r["amount_cents"]),
                    "unit_price": _d(r["unit_price_cents"]),
                }
            )
        return out

    def check_barcode(value: str) -> str:
        """Validate a receipt barcode at the boundary."""
        cleaned = value.strip()
        if not BARCODE.fullmatch(cleaned):
            raise ToolError(
                "bad_barcode",
                f"{value!r} is not a Costco receipt barcode.",
                "Use `transaction_barcode` from costco_list_receipts or a match result.",
            )
        return cleaned

    # ── tools ────────────────────────────────────────────────────────

    @mcp.tool(
        annotations=READ_ONLY,
        name="costco_get_sync_status",
        description=(
            "When the Costco receipt sync last succeeded, and which months it "
            "has actually fetched. Call this before reporting that a charge "
            "has no receipt behind it — an empty match and a sync that broke "
            "a week ago are indistinguishable otherwise."
        ),
    )
    async def get_sync_status() -> dict[str, Any]:
        try:
            fresh = await freshness()
            coverage = await db.fetch(
                "SELECT account, min(period) AS covered_from, max(period) AS covered_to,"
                " count(*) AS months FROM sync_coverage WHERE source = $1"
                " GROUP BY account ORDER BY account",
                _SOURCE,
            )
        except Exception as exc:  # noqa: BLE001 — mapped to the error contract
            return err(exc)
        return {
            **fresh,
            "stale_after_hours": settings.costco_stale_after_hours,
            "coverage": [row(r) for r in coverage],
        }

    @mcp.tool(
        annotations=READ_ONLY,
        name="costco_match_charges",
        description=(
            "Given bank charges from the ledger, find the Costco warehouse "
            "receipts and line items behind them. Batch: pull the rows with "
            "finances_transactions(payee_contains='costco') and hand them over "
            "in ONE call, keyed by your own `ref`. Matches on the TENDER, so a "
            "split-tender receipt resolves correctly. Returns a confidence per "
            "charge (exact / probable / ambiguous / none) and, when nothing "
            "matched, WHY. Amazon purchases are NOT in this data — use "
            "amazon_match_charges for those. Never picks among ambiguous "
            "candidates, and never suggests a category."
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
        window = settings.costco_match_window_days if window_days is None else window_days
        today = datetime.now(UTC).astimezone(zone).date()

        try:
            fresh = await freshness()
            cov_rows = await db.fetch(
                "SELECT DISTINCT period FROM sync_coverage WHERE source = $1", _SOURCE
            )
            covered = {r["period"] for r in cov_rows}

            results: list[dict[str, Any]] = []
            wanted: list[ReceiptKey] = []
            for ch in charges:
                day = parse_day(ch.date, field="date")
                cents = to_cents(ch.amount)

                # A REFUND has no counterpart here. Every receipt in the store
                # is `transaction_type = 'Sales'`; how Costco represents a
                # return has never been observed. Matching a positive ledger
                # amount against a same-amount purchase would confidently pair
                # a refund with an unrelated shopping trip, so it is refused
                # by name instead.
                if cents > 0:
                    results.append(
                        {
                            "ref": ch.ref,
                            "confidence": "none",
                            "reason": "refund_unsupported",
                            "candidates": [],
                        }
                    )
                    continue

                # Costco stores every amount POSITIVE, as it reports them;
                # Actual signs a spend negative. The comparison is still exact
                # and still in integers — only the sign convention differs, and
                # it differs from the amazon_* store, which signs charges the
                # way Actual does. See migration 0004's sign note.
                amount = abs(cents)
                cands = await db.fetch(
                    f"SELECT {', '.join(TENDER_COLUMNS)}"  # noqa: S608
                    " FROM costco_tenders t"
                    " JOIN costco_receipts r USING (account, transaction_barcode)"
                    " WHERE t.amount_cents = $1"
                    "   AND r.transaction_date BETWEEN $2 AND $3"
                    " ORDER BY r.transaction_date, t.account, t.id",
                    amount,
                    day - timedelta(days=window),
                    # A warehouse purchase settles at the register, so the
                    # receipt cannot post BEFORE the charge by more than clock
                    # skew — hence the tight, symmetric window.
                    day + timedelta(days=window),
                )
                expected = last4_by_account.get((ch.account or "").strip().lower())
                confidence, chosen = match_charge(
                    cents,
                    day,
                    [dict(c) for c in cands],
                    expected_last_4=expected,
                    last_4_key="card_last_4",
                    group_key="transaction_barcode",
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
                            "tender_id": c["tender_id"],
                            "account": c["account"],
                            "transaction_barcode": c["transaction_barcode"],
                            "date": c["transaction_date"].isoformat(),
                            "warehouse": c["warehouse_name"],
                            "tender": c["tender_description"],
                            "card_last_4": c["card_last_4"],
                            "amount": _d(c["amount_cents"]),
                            "receipt_total": _d(c["total_cents"]),
                            "item_count": c["total_item_count"],
                            # Above 1 means this receipt was paid with more
                            # than one tender, so `receipt_total` is NOT this
                            # charge and the other charges are elsewhere.
                            "tender_count": c["tender_count"],
                        }
                        for c in chosen
                    ]
                    wanted += [(c["account"], c["transaction_barcode"]) for c in chosen]
                results.append(entry)

            oversubscribed = flag_oversubscribed(results, id_key="tender_id")

            items = await items_for(sorted(set(wanted)), MAX_BATCH_ITEMS)
            for entry in results:
                for cand in entry.get("candidates", []):
                    cand["items"] = items.get((cand["account"], cand["transaction_barcode"]), [])
        except ToolError as exc:
            return exc.payload()
        except Exception as exc:  # noqa: BLE001 — mapped to the error contract
            return err(exc)

        return await stamped(
            {
                "results": results,
                "oversubscribed": oversubscribed,
                "match_window_days": window,
            }
        )

    @mcp.tool(
        annotations=READ_ONLY,
        name="costco_get_receipt",
        description=(
            "One warehouse receipt in full: every line item with its quantity "
            "and price, the tax and instant-savings totals, and how it was "
            "paid. Takes a `transaction_barcode` from costco_list_receipts or "
            "a match result."
        ),
    )
    async def get_receipt(
        transaction_barcode: Annotated[str, Field(description="Costco receipt barcode.")],
        account: Annotated[
            str | None, Field(default=None, description="Disambiguate a shared barcode.")
        ] = None,
    ) -> dict[str, Any]:
        try:
            barcode = check_barcode(transaction_barcode)
            receipts = await db.fetch(
                "SELECT account, transaction_barcode, transaction_datetime, transaction_date,"
                " warehouse_name, warehouse_number, total_cents, subtotal_cents, taxes_cents,"
                " instant_savings_cents, total_item_count, tender_count, transaction_type"
                " FROM costco_receipts WHERE transaction_barcode = $1"
                "   AND ($2::text IS NULL OR account = $2)"
                " ORDER BY account",
                barcode,
                account,
            )
            if not receipts:
                return await stamped(
                    {
                        "found": False,
                        "transaction_barcode": barcode,
                        "reason": "not_stored",
                        "receipts": [],
                    }
                )
            keys = [(r["account"], r["transaction_barcode"]) for r in receipts]
            items = await items_for(keys, MAX_ROWS)
            tenders = await db.fetch(
                "SELECT account, transaction_barcode, position, tender_description,"
                " card_last_4, amount_cents FROM costco_tenders"
                " WHERE (account, transaction_barcode) = ANY("
                "   SELECT unnest($1::text[]), unnest($2::text[]))"
                " ORDER BY account, transaction_barcode, position",
                [a for a, _ in keys],
                [b for _, b in keys],
            )
        except ToolError as exc:
            return exc.payload()
        except Exception as exc:  # noqa: BLE001 — mapped to the error contract
            return err(exc)

        out = []
        for r in receipts:
            key = (r["account"], r["transaction_barcode"])
            out.append(
                {
                    "account": r["account"],
                    "transaction_barcode": r["transaction_barcode"],
                    "date": r["transaction_date"].isoformat(),
                    # Warehouse-local wall clock; it carries no timezone by
                    # design, so it is rendered as stored rather than shifted.
                    "time": r["transaction_datetime"].strftime("%H:%M"),
                    "warehouse": r["warehouse_name"],
                    "warehouse_number": r["warehouse_number"],
                    "transaction_type": r["transaction_type"],
                    "total": _d(r["total_cents"]),
                    "subtotal": _d(r["subtotal_cents"]),
                    "taxes": _d(r["taxes_cents"]),
                    "instant_savings": _d(r["instant_savings_cents"]),
                    "item_count": r["total_item_count"],
                    "tender_count": r["tender_count"],
                    "items": items.get(key, []),
                    "tenders": [
                        {
                            "tender": t["tender_description"],
                            "card_last_4": t["card_last_4"],
                            "amount": _d(t["amount_cents"]),
                        }
                        for t in tenders
                        if (t["account"], t["transaction_barcode"]) == key
                    ],
                }
            )
        return await stamped({"found": True, "receipts": out})

    @mcp.tool(
        annotations=READ_ONLY,
        name="costco_search_items",
        description=(
            "Full-text search across Costco line items — 'when did we last buy "
            "olive oil, and what did it cost?'. Returns matching lines with "
            "their receipt date, warehouse and price."
        ),
    )
    async def search_items(
        query: Annotated[str, Field(min_length=2, description="Words to search for.")],
        limit: Annotated[int, Field(default=25, ge=1, le=MAX_ROWS)] = 25,
    ) -> dict[str, Any]:
        try:
            # websearch_to_tsquery, and the SAME to_tsvector expression as the
            # GIN index in lading's migration 0004 — a mismatch here does not
            # error, it silently drops to a sequential scan.
            rows = await db.fetch(
                "SELECT i.account, i.transaction_barcode, i.description, i.item_number,"
                " i.quantity, i.amount_cents, i.unit_price_cents,"
                " r.transaction_date, r.warehouse_name"
                " FROM costco_items i"
                " JOIN costco_receipts r USING (account, transaction_barcode)"
                " WHERE to_tsvector('english', i.description)"
                "       @@ websearch_to_tsquery('english', $1)"
                " ORDER BY r.transaction_date DESC, i.position LIMIT $2",
                query,
                limit,
            )
            total = await db.fetchval(
                "SELECT count(*) FROM costco_items"
                " WHERE to_tsvector('english', description)"
                "       @@ websearch_to_tsquery('english', $1)",
                query,
            )
        except Exception as exc:  # noqa: BLE001 — mapped to the error contract
            return err(exc)

        items = [
            {
                "description": r["description"],
                "item_number": r["item_number"],
                "quantity": r["quantity"],
                "amount": _d(r["amount_cents"]),
                "unit_price": _d(r["unit_price_cents"]),
                "date": r["transaction_date"].isoformat(),
                "warehouse": r["warehouse_name"],
                "account": r["account"],
                "transaction_barcode": r["transaction_barcode"],
            }
            for r in rows
        ]
        return await stamped(envelope(items, total or 0, "items"))

    @mcp.tool(
        annotations=READ_ONLY,
        name="costco_list_receipts",
        description=(
            "Recent warehouse receipts, newest first — the shopping trips "
            "themselves, without their line items. Use costco_get_receipt for "
            "what was in one."
        ),
    )
    async def list_receipts(
        since: Annotated[
            str | None, Field(default=None, description="Earliest date, 'YYYY-MM-DD'.")
        ] = None,
        limit: Annotated[int, Field(default=25, ge=1, le=MAX_ROWS)] = 25,
    ) -> dict[str, Any]:
        try:
            start = parse_day(since, field="since") if since else None
            rows = await db.fetch(
                "SELECT account, transaction_barcode, transaction_date, warehouse_name,"
                " total_cents, total_item_count, tender_count FROM costco_receipts"
                " WHERE ($1::date IS NULL OR transaction_date >= $1)"
                " ORDER BY transaction_date DESC, account LIMIT $2",
                start,
                limit,
            )
            total = await db.fetchval(
                "SELECT count(*) FROM costco_receipts"
                " WHERE ($1::date IS NULL OR transaction_date >= $1)",
                start,
            )
        except ToolError as exc:
            return exc.payload()
        except Exception as exc:  # noqa: BLE001 — mapped to the error contract
            return err(exc)

        receipts = [
            {
                "account": r["account"],
                "transaction_barcode": r["transaction_barcode"],
                "date": r["transaction_date"].isoformat(),
                "warehouse": r["warehouse_name"],
                "total": _d(r["total_cents"]),
                "item_count": r["total_item_count"],
                "tender_count": r["tender_count"],
            }
            for r in rows
        ]
        return await stamped(envelope(receipts, total or 0, "receipts"))
