"""Shared plumbing for tool categories that read a Postgres store.

Some categories proxy a live API (gatus, grocy, ha). A growing few instead
read a database owned by *another* service, which does the slow or fragile
work on a schedule:

    school_*  -> schoolhouse   (Schoology scrape)
    amazon_*  -> lading        (amazon.com scrape)

Those categories share a shape, and this module is that shape. It is
deliberately small: a read-only pool, a row renderer, the list envelope, and
the storage-failure mapping. Everything above it — the queries, the staleness
rules, what a tool is even called — stays in the category, because that is
where the domain lives.

Underscore prefix so `_registry` skips it: this exports no `register()`.

**Read-only by construction, not by convention.** Every consumer connects as
a role with `readonly` membership (CONNECT + USAGE + SELECT), so a bug here
cannot corrupt a store this server does not own. Keep it that way — there is
no `execute()` on :class:`Reader` and there should not be one.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime
from decimal import Decimal
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import asyncpg

from homelab_mcp.tools._http import ToolError

log = logging.getLogger(__name__)

# Bounded deliberately. These pools serve MCP reads, which are bursty and
# short; the writer lives in another process entirely and has its own pool.
MIN_POOL_SIZE = 1
MAX_POOL_SIZE = 4


class Reader:
    """A lazily-opened, read-only connection pool.

    Lazy because a category whose DSN is unset must not open a socket at
    import time, and because the server starts before Postgres is guaranteed
    up. The lock exists because two concurrent first requests would otherwise
    each build a pool and leak one.
    """

    def __init__(self, dsn: str) -> None:
        self._dsn = dsn
        self._pool: asyncpg.Pool[asyncpg.Record] | None = None
        self._lock = asyncio.Lock()

    async def _ensure(self) -> asyncpg.Pool[asyncpg.Record]:
        if self._pool is None:
            async with self._lock:
                if self._pool is None:
                    self._pool = await asyncpg.create_pool(
                        self._dsn, min_size=MIN_POOL_SIZE, max_size=MAX_POOL_SIZE
                    )
        return self._pool

    async def fetch(self, query: str, *args: Any) -> list[asyncpg.Record]:
        """Run a read returning rows."""
        pool = await self._ensure()
        rows: list[asyncpg.Record] = await pool.fetch(query, *args)
        return rows

    async def fetchrow(self, query: str, *args: Any) -> asyncpg.Record | None:
        """Run a read returning at most one row."""
        pool = await self._ensure()
        row: asyncpg.Record | None = await pool.fetchrow(query, *args)
        return row

    async def fetchval(self, query: str, *args: Any) -> Any:
        """Run a read returning a single scalar."""
        pool = await self._ensure()
        return await pool.fetchval(query, *args)


def load_zone(name: str, *, label: str) -> ZoneInfo:
    """Resolve a display timezone, falling back to UTC with a loud warning.

    `label` names the setting in the log line, so an operator reading
    "unknown lading timezone" knows which knob to fix without grepping.
    """
    try:
        return ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError):
        log.error("unknown %s timezone %r — displaying UTC", label, name)
        return ZoneInfo("UTC")


def render_row(record: Any, tz: ZoneInfo) -> dict[str, Any]:
    """Convert a database row to JSON-safe primitives, timestamps in `tz`.

    Storage is UTC; answers are local. A due date of 2026-09-15T23:59Z is
    7:59pm Eastern — which is also how Schoology renders an all-day item — so
    rendering UTC would make "due Thursday" wrong at the edges.

    NOTE the Decimal branch: it converts to float, which is fine for a grade
    percentage and WRONG for money. Categories dealing in currency should
    store integer cents (as `lading` does) so no Decimal ever reaches here; if
    one ever must, render it before calling this.
    """
    out: dict[str, Any] = {}
    for key, value in dict(record).items():
        if isinstance(value, Decimal):
            out[key] = float(value)
        elif isinstance(value, datetime):
            out[key] = value.astimezone(tz).isoformat()
        else:
            out[key] = value
    return out


def envelope(rows: list[dict[str, Any]], total: int, key: str) -> dict[str, Any]:
    """Wrap a list result so truncation is always explicit (AGENTS.md rule 6).

    A silently truncated list is worse than a short one: the model has no way
    to tell "these are all of them" from "these are the first twenty", and
    will confidently answer as though it saw everything.
    """
    return {
        key: rows,
        "returned": len(rows),
        "total": total,
        "truncated": len(rows) < total,
    }


def store_error(exc: Exception, *, code: str, store: str, env_var: str) -> ToolError:
    """Map an unexpected storage failure to the shared error contract.

    Only the exception CLASS is reported. An asyncpg failure message can carry
    the DSN, and that DSN carries a password.
    """
    log.warning("%s read failed: %s", store, exc.__class__.__name__)
    return ToolError(
        code,
        f"Could not read the {store} store ({exc.__class__.__name__}).",
        f"Check {env_var} and that Postgres is up.",
    )
