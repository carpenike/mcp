"""State store for the ARC Raiders tool category.

Two kinds of *personal/append-only* state, deliberately separate from the
OAuth store (game data and auth state must not share a file, a backup
story, or a blast radius):

  - **Raid log**. Append-only run history: map, loadout, intent, outcome,
    where you died, approximate loot value, notes. Append-only data is
    immune to the staleness problem that killed the stash-store idea —
    events don't rot, they accumulate. The analytics (extraction rate per
    loadout, death locations) are things no wiki can answer because
    they're personal, not general.

  - **Data snapshots**. Dated, hash-deduped captures of the upstream item
    table, taken opportunistically as a side effect of normal tool usage
    (max ~once per SNAPSHOT_MIN_AGE). Powers arc_patch_diff: "Kettle
    damage changed since last week" instead of "balance shifts every
    patch, check current tier lists".

Same concurrency pattern as oauth_state: one connection opened with
check_same_thread=False, every operation serialized under a single
asyncio.Lock. Set db_path to '' or ':memory:' for an ephemeral store
(raid history and snapshots then die with the process).
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import sqlite3
import time
from typing import Any

log = logging.getLogger(__name__)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS raid (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    ts          REAL NOT NULL,
    map         TEXT NOT NULL,
    loadout     TEXT,
    intent      TEXT,
    outcome     TEXT NOT NULL,
    died_at     TEXT,
    loot_value  INTEGER,
    notes       TEXT
);
CREATE INDEX IF NOT EXISTS raid_ts ON raid (ts);
CREATE TABLE IF NOT EXISTS snapshot (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    ts           REAL NOT NULL,
    kind         TEXT NOT NULL,
    content      TEXT NOT NULL,
    content_hash TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS snapshot_kind_ts ON snapshot (kind, ts);
CREATE TABLE IF NOT EXISTS player_state (
    season     TEXT NOT NULL,
    section    TEXT NOT NULL,
    content    TEXT NOT NULL,
    updated_at REAL NOT NULL,
    PRIMARY KEY (season, section)
);
CREATE TABLE IF NOT EXISTS state_meta (
    k TEXT PRIMARY KEY,
    v TEXT NOT NULL
);
"""

DEFAULT_SEASON = "s1"

RAID_OUTCOMES = ("extracted", "died", "disconnected")

# Don't store a new snapshot more often than this (seconds), and skip
# entirely when the content hash is unchanged.
SNAPSHOT_MIN_AGE = 20 * 3600


class ArcState:
    """SQLite-backed raid log + snapshot store for the arc_* tools."""

    def __init__(self, db_path: str) -> None:
        self._lock = asyncio.Lock()
        path = db_path or ":memory:"
        self._conn = sqlite3.connect(path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.executescript(_SCHEMA)
        self._conn.commit()

    # ── raid log ─────────────────────────────────────────────────────

    async def log_raid(
        self,
        *,
        map_name: str,
        outcome: str,
        loadout: str | None,
        intent: str | None,
        died_at: str | None,
        loot_value: int | None,
        notes: str | None,
        ts: float | None = None,
    ) -> int:
        """Append one raid; returns its id."""
        async with self._lock:
            cur = self._conn.execute(
                "INSERT INTO raid (ts, map, loadout, intent, outcome, died_at,"
                " loot_value, notes) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    ts if ts is not None else time.time(),
                    map_name,
                    loadout,
                    intent,
                    outcome,
                    died_at,
                    loot_value,
                    notes,
                ),
            )
            self._conn.commit()
            return int(cur.lastrowid or 0)

    async def get_raid(self, raid_id: int) -> dict[str, Any] | None:
        async with self._lock:
            row = self._conn.execute("SELECT * FROM raid WHERE id = ?", (raid_id,)).fetchone()
            return dict(row) if row else None

    async def delete_raid(self, raid_id: int) -> bool:
        """Remove one raid (correction path). True if a row was deleted."""
        async with self._lock:
            cur = self._conn.execute("DELETE FROM raid WHERE id = ?", (raid_id,))
            self._conn.commit()
            return cur.rowcount > 0

    async def list_raids(
        self, *, limit: int = 20, map_name: str | None = None
    ) -> list[dict[str, Any]]:
        """Most recent raids, optionally filtered by map (case-insensitive)."""
        async with self._lock:
            if map_name:
                rows = self._conn.execute(
                    "SELECT * FROM raid WHERE lower(map) = lower(?) ORDER BY ts DESC LIMIT ?",
                    (map_name, limit),
                ).fetchall()
            else:
                rows = self._conn.execute(
                    "SELECT * FROM raid ORDER BY ts DESC LIMIT ?", (limit,)
                ).fetchall()
            return [dict(r) for r in rows]

    async def raid_rows_since(self, cutoff_ts: float) -> list[dict[str, Any]]:
        """All raids newer than the cutoff (aggregation happens in the tool)."""
        async with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM raid WHERE ts >= ? ORDER BY ts DESC", (cutoff_ts,)
            ).fetchall()
            return [dict(r) for r in rows]

    # ── snapshots ────────────────────────────────────────────────────

    async def maybe_snapshot(self, kind: str, payload: dict[str, Any]) -> bool:
        """Store a dated snapshot unless too recent or content-identical.

        Returns True when a new snapshot row was written.
        """
        content = json.dumps(payload, sort_keys=True, separators=(",", ":"))
        content_hash = hashlib.sha256(content.encode()).hexdigest()
        async with self._lock:
            last = self._conn.execute(
                "SELECT ts, content_hash FROM snapshot WHERE kind = ? ORDER BY ts DESC LIMIT 1",
                (kind,),
            ).fetchone()
            now = time.time()
            if last is not None:
                if last["content_hash"] == content_hash:
                    return False
                if now - last["ts"] < SNAPSHOT_MIN_AGE:
                    return False
            self._conn.execute(
                "INSERT INTO snapshot (ts, kind, content, content_hash) VALUES (?, ?, ?, ?)",
                (now, kind, content, content_hash),
            )
            self._conn.commit()
            log.info("arcraiders snapshot stored: kind=%s bytes=%d", kind, len(content))
            return True

    async def latest_snapshot(self, kind: str) -> tuple[float, dict[str, Any]] | None:
        async with self._lock:
            row = self._conn.execute(
                "SELECT ts, content FROM snapshot WHERE kind = ? ORDER BY ts DESC LIMIT 1",
                (kind,),
            ).fetchone()
            return (row["ts"], json.loads(row["content"])) if row else None

    async def snapshot_at_or_before(
        self, kind: str, cutoff_ts: float
    ) -> tuple[float, dict[str, Any]] | None:
        """Newest snapshot no newer than the cutoff; falls back to the
        oldest available so a short history still yields a diff baseline."""
        async with self._lock:
            row = self._conn.execute(
                "SELECT ts, content FROM snapshot WHERE kind = ? AND ts <= ?"
                " ORDER BY ts DESC LIMIT 1",
                (kind, cutoff_ts),
            ).fetchone()
            if row is None:
                row = self._conn.execute(
                    "SELECT ts, content FROM snapshot WHERE kind = ? ORDER BY ts ASC LIMIT 1",
                    (kind,),
                ).fetchone()
            return (row["ts"], json.loads(row["content"])) if row else None

    async def snapshot_count(self, kind: str) -> int:
        async with self._lock:
            row = self._conn.execute(
                "SELECT COUNT(*) AS n FROM snapshot WHERE kind = ?", (kind,)
            ).fetchone()
            return int(row["n"])

    # ── player state (cross-device continuity) ───────────────────────
    # Sections are stored per-season, so a season reset is just moving
    # the current-season pointer — old seasons stay readable for
    # prestige comparisons, never deleted.

    async def current_season(self) -> str:
        async with self._lock:
            row = self._conn.execute(
                "SELECT v FROM state_meta WHERE k = 'current_season'"
            ).fetchone()
            return str(row["v"]) if row else DEFAULT_SEASON

    async def set_current_season(self, season: str) -> None:
        async with self._lock:
            self._conn.execute(
                "INSERT INTO state_meta (k, v) VALUES ('current_season', ?)"
                " ON CONFLICT(k) DO UPDATE SET v = excluded.v",
                (season,),
            )
            self._conn.commit()

    async def state_sections(self, season: str) -> dict[str, tuple[dict[str, Any] | str, float]]:
        """All stored sections for a season: name -> (content, updated_at)."""
        async with self._lock:
            rows = self._conn.execute(
                "SELECT section, content, updated_at FROM player_state WHERE season = ?",
                (season,),
            ).fetchall()
            return {r["section"]: (json.loads(r["content"]), r["updated_at"]) for r in rows}

    async def set_state_section(self, season: str, section: str, content: Any) -> float:
        """Upsert one section; returns the write timestamp."""
        now = time.time()
        async with self._lock:
            self._conn.execute(
                "INSERT INTO player_state (season, section, content, updated_at)"
                " VALUES (?, ?, ?, ?)"
                " ON CONFLICT(season, section) DO UPDATE SET"
                " content = excluded.content, updated_at = excluded.updated_at",
                (season, section, json.dumps(content, sort_keys=True), now),
            )
            self._conn.commit()
            return now

    async def delete_state_section(self, season: str, section: str) -> None:
        async with self._lock:
            self._conn.execute(
                "DELETE FROM player_state WHERE season = ? AND section = ?",
                (season, section),
            )
            self._conn.commit()

    async def known_seasons(self) -> list[str]:
        async with self._lock:
            rows = self._conn.execute(
                "SELECT DISTINCT season FROM player_state ORDER BY season"
            ).fetchall()
            return [str(r["season"]) for r in rows]
