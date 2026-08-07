"""Shared vocabulary for categories that explain a bank charge.

Two categories read a purchase store and answer the same question — *what was
this charge actually for?* — against different retailers:

    amazon_*  -> lading, amazon.com orders and transactions
    costco_*  -> lading, Costco warehouse receipts

Their DATA differs enough that merging the tools would be a mistake (an Amazon
charge is not an Amazon order; a Costco receipt is the charge; Amazon balance
has no Costco equivalent; Costco settles at the register). Their JUDGEMENT
must not differ at all. `exact`, `probable`, `ambiguous`, `none`,
`outside_coverage` and `oversubscribed` are promises made to a household about
its money, and they have to mean the same thing whichever store answered.

So this module holds the judgement and the units; each category keeps its own
vocabulary, its own caveats and its own tool surface.

Underscore prefix so `_registry` skips it: this exports no `register()`.

**Nothing here touches a database or reads a clock.** Every function is pure,
which is what lets both categories test the semantics without a store.
"""

from __future__ import annotations

from datetime import date
from typing import Any

from mcp.types import ToolAnnotations
from pydantic import BaseModel, ConfigDict, Field

from homelab_mcp.tools._http import ToolError

# Every tool in both categories is a read against a local database: read-only,
# idempotent, and closed-world *because the fetching happens in another
# service*. That is the concrete payoff of the split.
READ_ONLY = ToolAnnotations(
    readOnlyHint=True, destructiveHint=False, idempotentHint=True, openWorldHint=False
)

# Cap on one match batch. Fifty is already more than an advisor session hands
# over at once, and the batch has to be judged as a whole (see
# `flag_oversubscribed`), so it cannot be streamed.
MAX_CHARGES = 50


def dollars(cents: int | None) -> float | None:
    """Integer cents to dollars, rounded to the cent.

    Mirrors `finances._d`. Cents are authoritative and stay on the wire
    everywhere inside these modules; dollars are for the reader. Every amount
    comparison happens in integers — see :func:`match_charge`.
    """
    return None if cents is None else round(cents / 100.0, 2)


def to_cents(amount: float) -> int:
    """Dollars to integer cents, half-up, without trusting binary floats."""
    return int(round(amount * 100))


def parse_day(value: str, *, field: str) -> date:
    """Parse a YYYY-MM-DD date, or raise the shared error contract."""
    try:
        return date.fromisoformat(value.strip())
    except (ValueError, AttributeError):
        raise ToolError(
            "bad_date", f"{field} must be YYYY-MM-DD, got {value!r}.", "Example: 2026-08-04."
        ) from None


class Charge(BaseModel):
    """One bank charge to explain.

    `extra="forbid"` for the same reason `finances.Assignment` uses it: a
    caller that tries to pass a category, a payee or an instruction has
    misunderstood what these tools do, and should get a validation error
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


def match_charge(
    charge_cents: int,
    charge_day: date,
    candidates: list[dict[str, Any]],
    *,
    expected_last_4: str | None,
    last_4_key: str,
    group_key: str,
) -> tuple[str, list[dict[str, Any]]]:
    """Pick among same-amount candidates. Returns (confidence, chosen).

    Pure: no database, no clock. The SQL has already narrowed to rows whose
    amount matches EXACTLY and whose date is inside the window; this decides
    how much to believe the result.

    `last_4_key` and `group_key` name the caller's own columns, and are
    deliberately required rather than defaulted to Amazon's names — a default
    here would make one source the "real" one and let the other drift into a
    silent mismatch. Amazon groups by `order_number`, Costco by
    `transaction_barcode`; both mean "candidates that are the same purchase".

    The last-4 filter is applied only when it can actually be applied — the
    caller named an account, that account has a configured card, and the
    candidate row knows its card. Any of those missing lowers confidence
    instead of dropping candidates: never discard a row over a comparison you
    could not make.
    """
    if not candidates:
        return "none", []

    usable = [c for c in candidates if c.get(last_4_key)]
    if expected_last_4 and usable:
        narrowed = [c for c in usable if c[last_4_key] == expected_last_4]
        if narrowed:
            if len({c.get(group_key) for c in narrowed}) == 1:
                return "exact", narrowed
            return "ambiguous", narrowed
        # The account's card is known and NO candidate matches it. That is
        # evidence against every candidate, not for one of them.
        return "ambiguous", candidates

    groups = {c.get(group_key) for c in candidates}
    if len(groups) == 1:
        # One purchase, whether that is one candidate or several charges
        # against the same one. Not `exact`: the card was never verified.
        return "probable", candidates
    return "ambiguous", candidates


def flag_oversubscribed(results: list[dict[str, Any]], *, id_key: str) -> int:
    """Mark charges that competed for too few source rows. Returns how many.

    Pure, and deliberately separate from :func:`match_charge`: this is the one
    judgement that cannot be made per charge, which is why charges are handed
    over as a batch at all.

    A flagged entry is downgraded to `ambiguous` because the truthful answer
    is that we cannot tell which charge owns which row — and at least one of
    them owns none of them. Leaving it at `probable` would present a wrong
    answer with a confident label, which is the failure mode these categories
    exist to avoid.
    """
    claims: dict[Any, set[str]] = {}
    for entry in results:
        for cand in entry.get("candidates", []):
            claims.setdefault(cand[id_key], set()).add(entry["ref"])

    flagged = 0
    for entry in results:
        ids = {c[id_key] for c in entry.get("candidates", [])}
        if not ids:
            continue
        sharers: set[str] = set()
        for i in ids:
            sharers |= claims[i]
        if len(sharers) > len(ids):
            entry["oversubscribed"] = True
            entry["shares_with"] = sorted(sharers - {entry["ref"]})
            entry["confidence"] = "ambiguous"
            flagged += 1
    return flagged


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
