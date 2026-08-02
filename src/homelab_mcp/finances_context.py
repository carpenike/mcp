"""Store for what humans say about transactions, in their own words.

The ledger records that $266.14 went to Bavarian Inn. It cannot record that
it was Jayme's birthday dinner. That sentence exists for about a day — in a
Signal reply, or mid-conversation — and then nobody remembers. This is where
it lands, verbatim, at the moment it is said.

Why SQLite rather than the governance repo (`finances_docs`): this is
data-shaped and append-heavy — many small rows, queried by status and matched
against transactions — not prose a human reads top-to-bottom. It follows the
arcraiders_state precedent, and lives in its own file for the same reason
that one does: personal context and auth state must not share a blast radius.

Three properties the design turns on:

  * **`txn_ref` is a HINT, not a foreign key.** The whole point is capturing
    a statement before the purchase posts to the bank feed, so there may be
    no Actual transaction to reference yet. Date/amount/payee are free-form
    matching material for later, and a row with none of them is still worth
    keeping.

  * **Aging is lazy and non-destructive.** Open rows past the window become
    `aged_out` when something next reads them — no background job. They are
    never deleted: silence is allowed to mean "nobody remembers" without the
    queue growing forever, and triage can still go looking later.

  * **hermes may write here and nowhere else.** This is its single writable
    surface: it can record what someone replied, and can never act on the
    ledger with it.
"""

from __future__ import annotations

import asyncio
import json
import logging
import sqlite3
import time
from typing import Any

log = logging.getLogger(__name__)

# Open context older than this is assumed forgotten rather than pending.
AGE_OUT_SECONDS = 45 * 86400
# Per-author daily cap. Bounds the blast radius of a loop or a stuck agent
# without ever getting near what a human would type in a day.
DEFAULT_DAILY_LIMIT = 20

STATUSES = ("open", "consumed", "aged_out")
SOURCES = ("pulse_clarify", "volunteered")

_SCHEMA = """
CREATE TABLE IF NOT EXISTS txn_context (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at   REAL NOT NULL,
    author       TEXT NOT NULL,
    ref_date     TEXT,
    ref_amount   REAL,
    ref_payee    TEXT,
    note         TEXT NOT NULL,
    source       TEXT NOT NULL,
    status       TEXT NOT NULL DEFAULT 'open',
    consumed_by  TEXT,
    consumed_at  REAL
);
CREATE INDEX IF NOT EXISTS txn_context_status ON txn_context (status, created_at);
CREATE INDEX IF NOT EXISTS txn_context_author ON txn_context (author, created_at);
"""


def _row(r: sqlite3.Row) -> dict[str, Any]:
    """Project a row into the shape tools return."""
    return {
        "id": r["id"],
        "created_at": r["created_at"],
        "author": r["author"],
        "txn_ref": {
            "date": r["ref_date"],
            "amount": r["ref_amount"],
            "payee_hint": r["ref_payee"],
        },
        "note": r["note"],
        "source": r["source"],
        "status": r["status"],
        "consumed_by": r["consumed_by"],
        "consumed_at": r["consumed_at"],
    }


class ContextStore:
    """SQLite-backed transaction-context store."""

    def __init__(self, db_path: str, daily_limit: int = DEFAULT_DAILY_LIMIT) -> None:
        self._lock = asyncio.Lock()
        self._conn = sqlite3.connect(db_path or ":memory:", check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.executescript(_SCHEMA)
        self._conn.commit()
        self.daily_limit = daily_limit

    # ── aging ────────────────────────────────────────────────────────
    def _age_out(self, now: float) -> int:
        """Retire stale open rows. Caller holds the lock.

        Lazy rather than scheduled: a background job would be a second moving
        part to deploy and monitor for something that only needs to be true
        whenever somebody looks.
        """
        cur = self._conn.execute(
            "UPDATE txn_context SET status = 'aged_out' WHERE status = 'open' AND created_at < ?",
            (now - AGE_OUT_SECONDS,),
        )
        if cur.rowcount:
            self._conn.commit()
        return int(cur.rowcount or 0)

    # ── writes ───────────────────────────────────────────────────────
    async def add(
        self,
        *,
        author: str,
        note: str,
        source: str,
        ref_date: str | None,
        ref_amount: float | None,
        ref_payee: str | None,
        created_at: float | None = None,
    ) -> dict[str, Any]:
        now = time.time() if created_at is None else created_at
        async with self._lock:
            used = int(
                self._conn.execute(
                    "SELECT COUNT(*) FROM txn_context WHERE author = ? AND created_at > ?",
                    (author, time.time() - 86400),
                ).fetchone()[0]
            )
            if used >= self.daily_limit:
                raise RateLimitedError(author, used, self.daily_limit)
            cur = self._conn.execute(
                "INSERT INTO txn_context (created_at, author, ref_date, ref_amount,"
                " ref_payee, note, source, status) VALUES (?, ?, ?, ?, ?, ?, ?, 'open')",
                (now, author, ref_date, ref_amount, ref_payee, note, source),
            )
            self._conn.commit()
            row = self._conn.execute(
                "SELECT * FROM txn_context WHERE id = ?", (cur.lastrowid,)
            ).fetchone()
            return _row(row)

    async def consume(self, ids: list[int], by: str, note: str | None) -> dict[str, Any]:
        """Mark rows consumed. Only `open` rows transition."""
        async with self._lock:
            found = {
                int(r["id"]): r["status"]
                for r in self._conn.execute(
                    f"SELECT id, status FROM txn_context WHERE id IN ({','.join('?' * len(ids))})",  # noqa: S608 - placeholders only
                    ids,
                ).fetchall()
            }
            missing = [i for i in ids if i not in found]
            already = [i for i, st in found.items() if st != "open"]
            target = [i for i, st in found.items() if st == "open"]
            if target:
                now = time.time()
                self._conn.executemany(
                    "UPDATE txn_context SET status = 'consumed', consumed_by = ?,"
                    " consumed_at = ?, note = note || ? WHERE id = ?",
                    [(by, now, f"\n\n[consumed] {note}" if note else "", i) for i in target],
                )
                self._conn.commit()
            return {"consumed": target, "missing": missing, "not_open": already}

    # ── reads ────────────────────────────────────────────────────────
    async def list_entries(
        self, status: str | None, limit: int
    ) -> tuple[list[dict[str, Any]], int]:
        async with self._lock:
            aged = self._age_out(time.time())
            if status:
                rows = self._conn.execute(
                    "SELECT * FROM txn_context WHERE status = ? ORDER BY created_at DESC LIMIT ?",
                    (status, limit + 1),
                ).fetchall()
            else:
                rows = self._conn.execute(
                    "SELECT * FROM txn_context ORDER BY created_at DESC LIMIT ?", (limit + 1,)
                ).fetchall()
            return [_row(r) for r in rows[:limit]], aged

    async def open_refs(self) -> list[dict[str, Any]]:
        """Open rows, for excluding transactions already asked about."""
        async with self._lock:
            self._age_out(time.time())
            return [
                _row(r)
                for r in self._conn.execute(
                    "SELECT * FROM txn_context WHERE status = 'open'"
                ).fetchall()
            ]

    async def remaining_today(self, author: str) -> int:
        async with self._lock:
            used = int(
                self._conn.execute(
                    "SELECT COUNT(*) FROM txn_context WHERE author = ? AND created_at > ?",
                    (author, time.time() - 86400),
                ).fetchone()[0]
            )
            return max(self.daily_limit - used, 0)


class RateLimitedError(Exception):
    """Raised when an author exceeds the daily add cap."""

    def __init__(self, author: str, used: int, limit: int) -> None:
        super().__init__(f"{author} has used {used}/{limit} context entries today")
        self.author = author
        self.used = used
        self.limit = limit


def matches_ref(ref: dict[str, Any], txn: dict[str, Any]) -> bool:
    """Whether an open context row plausibly refers to this transaction.

    Deliberately loose on any single field and strict about needing agreement:
    the hints were typed by a human who may have rounded the amount or
    remembered the day wrong. Amount is matched within a dollar, date within
    three days, payee as a case-insensitive substring either way round. A ref
    with no usable hint at all never matches anything — it is still worth
    storing, but it cannot silently suppress a clarify candidate.
    """
    amount, ref_date, payee = ref.get("amount"), ref.get("date"), ref.get("payee_hint")
    if amount is None and not ref_date and not payee:
        return False

    # Amount within a dollar: a human recalling "about 266" should still match.
    if amount is not None and abs(abs(float(amount)) - abs(float(txn["amount"]))) > 1.0:
        return False
    if ref_date:
        try:
            from datetime import date as _date

            delta = abs((_date.fromisoformat(ref_date) - _date.fromisoformat(txn["date"])).days)
        except (TypeError, ValueError):
            return False
        if delta > 3:
            return False
    if payee:
        a, b = str(payee).lower().strip(), str(txn.get("payee") or "").lower()
        if not a:
            return False
        if a not in b and b not in a:
            return False
    return True


def dumps(value: Any) -> str:
    """Stable JSON, for audit lines."""
    return json.dumps(value, sort_keys=True, separators=(",", ":"))
