"""School tools — the household's Schoology data, read-only.

This category is a READER over a database owned by another service.
`schoolhouse` (github:carpenike/schoolhouse) does the fragile half: logs into
Schoology twice a day as the parent, fetches, parses, and writes an
append-only history into its own Postgres database. Nothing in this file
fetches anything from Schoology, and nothing here writes.

Two deliberate properties:

* **The connection is a read-only role.** `HOMELAB_MCP_SCHOOLHOUSE_DATABASE_URL`
  points at a login role holding `readonly` membership (CONNECT + USAGE +
  SELECT). This process cannot corrupt a child's grade history even if a bug
  in this file tried to.
* **This category is opt-in per client.** These tools expose three minors'
  education records. Keep `school_*` OUT of the `hermes` entry in
  `HOMELAB_MCP_RESTRICTED_SCOPES` so the ambient household agent cannot read
  them; the OAuth user allowlist already limits interactive access to the
  two parents.

If `HOMELAB_MCP_SCHOOLHOUSE_DATABASE_URL` is unset the category simply does
not register — same as any other unconfigured upstream.

Tool name convention: `school_<verb>_<object>`. See AGENTS.md.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Annotated, Any
from zoneinfo import ZoneInfo

from mcp.types import ToolAnnotations
from pydantic import Field

from homelab_mcp.tools._http import ToolError
from homelab_mcp.tools._pg import Reader, envelope, load_zone, render_row, store_error

if TYPE_CHECKING:
    from mcp.server.fastmcp import FastMCP

    from homelab_mcp.config import Settings

log = logging.getLogger(__name__)

# Every tool here is a read against a local database: read-only, idempotent,
# closed-world. Truthful, not convenient — see AGENTS.md.
_RO = ToolAnnotations(
    readOnlyHint=True, destructiveHint=False, idempotentHint=True, openWorldHint=False
)

# Hard cap on any list response. Three children's coursework runs to thousands
# of rows; a truncated-and-flagged answer beats flooding the context.
MAX_ROWS = 200

INSTRUCTIONS = """
The school_* tools expose the household children's Schoology coursework,
read-only, from a twice-daily sync.

Freshness: nothing here is live. Every response carries `data_as_of` and
`stale`. Say "as of this morning" rather than implying you just checked, and
if `stale` is true say so instead of presenting old data as current.

Before telling anyone a child has no missing work or nothing due, call
school_get_sync_status. "Nothing found" and "the sync has been broken for a
week" are indistinguishable in the data otherwise.

Missing work has two bases: `observed` (Schoology itself said missing) and
`inferred_past_due` (due date passed, nothing submitted recorded). Report the
difference — an inferred item is often just ungraded.

Courses carry a teacher name; `teacher_uid` is set only when the name matched
exactly one member of the school faculty directory. A blank one with a
`teacher_link_note` means the surname was ambiguous, not that the teacher is
unknown.
""".strip()


# The Postgres plumbing moved to `_pg` when `amazon_*` became the second
# category reading a sibling service's store. Re-exported here so existing
# imports (and tests/test_tools_school.py) keep working — the names are part
# of this module's surface, the implementation just is not.
_Reader = Reader

_STORE = "schoolhouse"
_STORE_ENV = "HOMELAB_MCP_SCHOOLHOUSE_DATABASE_URL"


def _load_zone(name: str) -> ZoneInfo:
    """Resolve the display timezone, falling back to UTC with a loud warning."""
    return load_zone(name, label=_STORE)


def _store_error(exc: Exception) -> ToolError:
    """Map an unexpected storage failure to the shared error contract."""
    return store_error(exc, code="schoolhouse_unreachable", store=_STORE, env_var=_STORE_ENV)


def register(mcp: FastMCP, settings: Settings) -> None:
    """Register school_* tools, if a schoolhouse database is configured."""
    dsn = settings.schoolhouse_database_url
    if not dsn:
        log.info("HOMELAB_MCP_SCHOOLHOUSE_DATABASE_URL unset — school tools not registered")
        return

    db = _Reader(dsn)
    zone = _load_zone(settings.schoolhouse_timezone)

    def row(record: Any) -> dict[str, Any]:
        return render_row(record, zone)

    # ── helpers ──────────────────────────────────────────────────────

    async def child_ids(child: str | None) -> list[int] | None:
        """Resolve a child name or id to database ids. None means 'all'.

        Child scoping is enforced here, in SQL, rather than by asking the
        model to filter. If per-child access control is ever wanted, this is
        the one place it goes.
        """
        if not child or child.strip().lower() in {"all", "everyone", "*"}:
            return None
        needle = child.strip().lower()
        rows = await db.fetch(
            "SELECT id, schoology_user_id, display_name FROM children WHERE active"
        )
        matches = [
            int(r["id"])
            for r in rows
            if r["schoology_user_id"] == child.strip()
            or str(r["display_name"]).lower() == needle
            or str(r["display_name"]).lower().split(" ")[0] == needle
        ]
        if not matches:
            known = ", ".join(sorted(str(r["display_name"]) for r in rows)) or "(none yet)"
            raise ToolError(
                "unknown_child",
                f"No child matches {child!r}.",
                f"Known children: {known}. Call school_list_children first.",
            )
        return matches

    async def freshness() -> dict[str, Any]:
        """Most recent successful ingest, overall and per source."""
        rows = await db.fetch(
            "SELECT DISTINCT ON (source) source, finished_at, status, records_changed,"
            " parsers_pending, error FROM ingest_runs"
            " WHERE finished_at IS NOT NULL AND source <> 'seed'"
            " ORDER BY source, finished_at DESC"
        )
        ok = [
            r["finished_at"]
            for r in rows
            if r["status"] in ("ok", "partial") and r["finished_at"] is not None
        ]
        latest: datetime | None = max(ok) if ok else None
        age_hours = None if latest is None else (datetime.now(UTC) - latest).total_seconds() / 3600
        return {
            "data_as_of": None if latest is None else latest.astimezone(zone).isoformat(),
            "stale": latest is None or (age_hours or 0) > settings.schoolhouse_stale_after_hours,
            "age_hours": None if age_hours is None else round(age_hours, 1),
            "sources": [row(r) for r in rows],
        }

    async def stamped(payload: dict[str, Any]) -> dict[str, Any]:
        """Attach the freshness marker every response carries."""
        fresh = await freshness()
        payload["data_as_of"] = fresh["data_as_of"]
        payload["stale"] = fresh["stale"]
        return payload

    # ── tools ────────────────────────────────────────────────────────

    @mcp.tool(
        annotations=_RO,
        name="school_list_children",
        description=(
            "List the children tracked by the household's Schoology sync, "
            "with active course counts and when each was last synced. Call "
            "this first when you need a child's name for another tool, or "
            "when a request is ambiguous about which kid it means."
        ),
    )
    async def list_children() -> dict[str, Any]:
        try:
            rows = await db.fetch(
                "SELECT ch.schoology_user_id, ch.display_name, ch.active,"
                " (SELECT count(*) FROM courses c WHERE c.child_id = ch.id AND c.active)"
                "   AS course_count,"
                " (SELECT max(f.fetched_at) FROM raw_fetches f"
                "   JOIN ingest_runs r ON r.id = f.run_id"
                "   WHERE f.child_key = ch.schoology_user_id"
                "     AND r.status IN ('ok', 'partial')) AS last_sync"
                " FROM children ch ORDER BY ch.display_name"
            )
        except Exception as exc:  # noqa: BLE001 — never raise to the transport
            return _store_error(exc).payload()
        return await stamped(envelope([row(r) for r in rows], len(rows), "children"))

    @mcp.tool(
        annotations=_RO,
        name="school_list_courses",
        description=(
            "List active courses with teacher, period and the most recent "
            "course grade. Omit `child` for all children. For how one grade "
            "moved over time use school_get_grades with `since`."
        ),
    )
    async def list_courses(
        child: Annotated[str | None, Field(description="Child name or Schoology id")] = None,
    ) -> dict[str, Any]:
        try:
            ids = await child_ids(child)
            rows = await db.fetch(
                "SELECT ch.display_name AS child, c.schoology_section_id, c.title, c.teacher,"
                " c.teacher_uid, s.display_name AS teacher_full_name, c.teacher_link_note,"
                " c.period, c.term, g.percent, g.letter, g.grading_period,"
                " g.observed_at AS grade_observed_at"
                " FROM courses c"
                " JOIN children ch ON ch.id = c.child_id"
                " LEFT JOIN staff s ON s.schoology_uid = c.teacher_uid"
                " LEFT JOIN LATERAL ("
                "   SELECT percent, letter, grading_period, observed_at"
                "   FROM latest_course_grade lg WHERE lg.course_id = c.id"
                "   ORDER BY observed_at DESC LIMIT 1) g ON true"
                " WHERE c.active AND ($1::int[] IS NULL OR c.child_id = ANY($1))"
                " ORDER BY ch.display_name, c.period, c.title LIMIT $2",
                ids,
                MAX_ROWS,
            )
            total = await db.fetchval(
                "SELECT count(*) FROM courses c"
                " WHERE c.active AND ($1::int[] IS NULL OR c.child_id = ANY($1))",
                ids,
            )
        except ToolError as err:
            return err.payload()
        except Exception as exc:  # noqa: BLE001
            return _store_error(exc).payload()
        return await stamped(envelope([row(r) for r in rows], int(total), "courses"))

    @mcp.tool(
        annotations=_RO,
        name="school_get_upcoming_work",
        description=(
            "Assignments due within the next N days, soonest first, with the "
            "last observed status. Omit `child` for all children — this is "
            "the tool for 'who has what due this week'."
        ),
    )
    async def get_upcoming_work(
        child: Annotated[str | None, Field(description="Child name or Schoology id")] = None,
        days: Annotated[int, Field(ge=1, le=90)] = 7,
    ) -> dict[str, Any]:
        try:
            ids = await child_ids(child)
            rows = await db.fetch(
                "SELECT ch.display_name AS child, c.title AS course, a.schoology_assignment_id,"
                " a.title, a.due_at, a.points_possible, a.category, a.url,"
                " COALESCE(s.status, 'assigned') AS status"
                " FROM assignments a"
                " JOIN courses c ON c.id = a.course_id"
                " JOIN children ch ON ch.id = c.child_id"
                " LEFT JOIN latest_assignment_state s ON s.assignment_id = a.id"
                " WHERE a.due_at IS NOT NULL AND a.due_at >= now()"
                "   AND a.due_at < now() + ($2 || ' days')::interval"
                "   AND ($1::int[] IS NULL OR c.child_id = ANY($1))"
                " ORDER BY a.due_at, ch.display_name LIMIT $3",
                ids,
                str(days),
                MAX_ROWS,
            )
            total = await db.fetchval(
                "SELECT count(*) FROM assignments a JOIN courses c ON c.id = a.course_id"
                " WHERE a.due_at IS NOT NULL AND a.due_at >= now()"
                "   AND a.due_at < now() + ($2 || ' days')::interval"
                "   AND ($1::int[] IS NULL OR c.child_id = ANY($1))",
                ids,
                str(days),
            )
        except ToolError as err:
            return err.payload()
        except Exception as exc:  # noqa: BLE001
            return _store_error(exc).payload()
        payload = envelope([row(r) for r in rows], int(total), "assignments")
        payload["window_days"] = days
        return await stamped(payload)

    @mcp.tool(
        annotations=_RO,
        name="school_get_missing_work",
        description=(
            "Work that is missing or overdue, with days past due. Each item "
            "carries a `basis`: 'observed' means Schoology reported it "
            "missing; 'inferred_past_due' means the due date passed with "
            "nothing submitted recorded, which often just means ungraded. "
            "Report the difference rather than merging them."
        ),
    )
    async def get_missing_work(
        child: Annotated[str | None, Field(description="Child name or Schoology id")] = None,
    ) -> dict[str, Any]:
        try:
            ids = await child_ids(child)
            rows = await db.fetch(
                "SELECT ch.display_name AS child, c.title AS course,"
                " a.schoology_assignment_id, a.title, a.due_at, a.points_possible, a.url,"
                " COALESCE(s.status, 'unknown') AS status,"
                " COALESCE(s.days_past_due,"
                "          GREATEST(0, (EXTRACT(EPOCH FROM now() - a.due_at) / 86400)::int))"
                "   AS days_past_due,"
                " CASE WHEN s.status = 'missing' THEN 'observed'"
                "      ELSE 'inferred_past_due' END AS basis"
                " FROM assignments a"
                " JOIN courses c ON c.id = a.course_id"
                " JOIN children ch ON ch.id = c.child_id"
                " LEFT JOIN latest_assignment_state s ON s.assignment_id = a.id"
                " WHERE ($1::int[] IS NULL OR c.child_id = ANY($1))"
                "   AND (s.status = 'missing'"
                "        OR (a.due_at IS NOT NULL AND a.due_at < now()"
                "            AND COALESCE(s.status, 'assigned') IN ('assigned', 'unknown')))"
                " ORDER BY a.due_at DESC LIMIT $2",
                ids,
                MAX_ROWS,
            )
        except ToolError as err:
            return err.payload()
        except Exception as exc:  # noqa: BLE001
            return _store_error(exc).payload()
        items = [row(r) for r in rows]
        payload = envelope(items, len(items), "missing")
        payload["observed"] = sum(1 for i in items if i["basis"] == "observed")
        payload["inferred"] = sum(1 for i in items if i["basis"] == "inferred_past_due")
        return await stamped(payload)

    @mcp.tool(
        annotations=_RO,
        name="school_get_grades",
        description=(
            "Current course grades, or the full trajectory when `since` is "
            "given (ISO date, interpreted in local time). Use `since` for "
            "'how has her math grade moved this quarter' — the store keeps "
            "every observed change, so the series is real history."
        ),
    )
    async def get_grades(
        child: Annotated[str | None, Field(description="Child name or Schoology id")] = None,
        course: Annotated[str | None, Field(description="Course title substring")] = None,
        since: Annotated[str | None, Field(description="ISO date, e.g. 2026-09-01")] = None,
    ) -> dict[str, Any]:
        try:
            ids = await child_ids(child)
            since_dt = _parse_since(since, zone)
            if since_dt is None:
                rows = await db.fetch(
                    "SELECT ch.display_name AS child, c.title AS course, g.grading_period,"
                    " g.percent, g.letter, g.observed_at"
                    " FROM latest_course_grade g"
                    " JOIN courses c ON c.id = g.course_id"
                    " JOIN children ch ON ch.id = c.child_id"
                    " WHERE ($1::int[] IS NULL OR c.child_id = ANY($1))"
                    "   AND ($2::text IS NULL OR c.title ILIKE '%' || $2 || '%')"
                    " ORDER BY ch.display_name, c.title LIMIT $3",
                    ids,
                    course,
                    MAX_ROWS,
                )
            else:
                rows = await db.fetch(
                    "SELECT ch.display_name AS child, c.title AS course, g.grading_period,"
                    " g.percent, g.letter, g.observed_at"
                    " FROM course_grades g"
                    " JOIN courses c ON c.id = g.course_id"
                    " JOIN children ch ON ch.id = c.child_id"
                    " WHERE ($1::int[] IS NULL OR c.child_id = ANY($1))"
                    "   AND ($2::text IS NULL OR c.title ILIKE '%' || $2 || '%')"
                    "   AND g.observed_at >= $3"
                    " ORDER BY ch.display_name, c.title, g.observed_at LIMIT $4",
                    ids,
                    course,
                    since_dt,
                    MAX_ROWS,
                )
        except ToolError as err:
            return err.payload()
        except Exception as exc:  # noqa: BLE001
            return _store_error(exc).payload()
        payload = envelope([row(r) for r in rows], len(rows), "grades")
        payload["series"] = since_dt is not None
        return await stamped(payload)

    @mcp.tool(
        annotations=_RO,
        name="school_get_assignment",
        description=(
            "Full detail for one assignment: `history` is every status and "
            "score change recorded, `revisions` is every time the teacher "
            "edited the due date, title or points. Both oldest first. "
            "Answers 'when did this get marked missing' and 'was this due "
            "yesterday or did it get pushed'."
        ),
    )
    async def get_assignment(
        assignment_id: Annotated[str, Field(description="Schoology assignment id (numeric)")],
    ) -> dict[str, Any]:
        try:
            if not assignment_id.strip().isdigit():
                raise ToolError(
                    "bad_assignment_id",
                    "Assignment ids are numeric.",
                    "Use the schoology_assignment_id from school_get_upcoming_work.",
                )
            record = await db.fetchrow(
                "SELECT ch.display_name AS child, c.title AS course, a.id,"
                " a.schoology_assignment_id, a.title, a.due_at, a.points_possible,"
                " a.category, a.url, a.first_seen_at, a.last_seen_at"
                " FROM assignments a"
                " JOIN courses c ON c.id = a.course_id"
                " JOIN children ch ON ch.id = c.child_id"
                " WHERE a.schoology_assignment_id = $1 LIMIT 1",
                assignment_id.strip(),
            )
            if record is None:
                raise ToolError(
                    "assignment_not_found",
                    f"No assignment {assignment_id!r} in the store.",
                    "It may predate the first sync, or belong to an untracked course.",
                )
            history = await db.fetch(
                "SELECT observed_at, status, points_earned, grade_pct, days_past_due, comment"
                " FROM assignment_states WHERE assignment_id = $1 ORDER BY observed_at LIMIT $2",
                int(record["id"]),
                MAX_ROWS,
            )
            revisions = await db.fetch(
                "SELECT observed_at, title, due_at, points_possible"
                " FROM assignment_revisions WHERE assignment_id = $1"
                " ORDER BY observed_at LIMIT $2",
                int(record["id"]),
                MAX_ROWS,
            )
        except ToolError as err:
            return err.payload()
        except Exception as exc:  # noqa: BLE001
            return _store_error(exc).payload()
        detail = row(record)
        detail.pop("id", None)
        detail["history"] = [row(h) for h in history]
        detail["revisions"] = [row(r) for r in revisions]
        detail["was_edited"] = len(revisions) > 1
        return await stamped(detail)

    @mcp.tool(
        annotations=_RO,
        name="school_get_announcements",
        description=(
            "Recent teacher and school announcements from the Schoology "
            "activity feed, newest first. Filter with `course` for one "
            "class. Use when asked what a teacher said, or whether anything "
            "was announced about a test or a trip. `context_kind` separates "
            "a course post from a school-wide notice."
        ),
    )
    async def get_announcements(
        child: Annotated[str | None, Field(description="Child name or Schoology id")] = None,
        days: Annotated[int, Field(ge=1, le=365)] = 14,
        course: Annotated[str | None, Field(description="Course title substring")] = None,
    ) -> dict[str, Any]:
        try:
            ids = await child_ids(child)
            rows = await db.fetch(
                "SELECT ch.display_name AS child, u.schoology_update_id, u.posted_at,"
                " u.author_name, u.context_kind, c.title AS course, u.body,"
                " u.attachment_count, (u.edited_count > 0) AS edited"
                " FROM updates u"
                " JOIN children ch ON ch.id = u.child_id"
                " LEFT JOIN courses c ON c.id = u.course_id"
                " WHERE ($1::int[] IS NULL OR u.child_id = ANY($1))"
                "   AND ($2::text IS NULL OR c.title ILIKE '%' || $2 || '%')"
                "   AND (u.posted_at IS NULL OR u.posted_at >= now() - ($3 || ' days')::interval)"
                " ORDER BY u.posted_at DESC NULLS LAST LIMIT $4",
                ids,
                course,
                str(days),
                MAX_ROWS,
            )
            total = await db.fetchval(
                "SELECT count(*) FROM updates u LEFT JOIN courses c ON c.id = u.course_id"
                " WHERE ($1::int[] IS NULL OR u.child_id = ANY($1))"
                "   AND ($2::text IS NULL OR c.title ILIKE '%' || $2 || '%')"
                "   AND (u.posted_at IS NULL OR u.posted_at >= now() - ($3 || ' days')::interval)",
                ids,
                course,
                str(days),
            )
        except ToolError as err:
            return err.payload()
        except Exception as exc:  # noqa: BLE001
            return _store_error(exc).payload()
        payload = envelope([row(r) for r in rows], int(total), "announcements")
        payload["window_days"] = days
        return await stamped(payload)

    @mcp.tool(
        annotations=_RO,
        name="school_list_staff",
        description=(
            "Look up school staff by name, with the courses they teach for "
            "these children. Use when someone names a teacher. `courses` is "
            "empty when a staff member teaches none of these children — the "
            "directory covers the whole school."
        ),
    )
    async def list_staff(
        query: Annotated[str | None, Field(description="Name substring")] = None,
    ) -> dict[str, Any]:
        try:
            rows = await db.fetch(
                "SELECT s.display_name, s.schoology_uid, s.role, s.school_id,"
                " COALESCE(array_agg(c.title ORDER BY c.title)"
                "          FILTER (WHERE c.title IS NOT NULL), '{}') AS courses"
                " FROM staff s"
                " LEFT JOIN courses c ON c.teacher_uid = s.schoology_uid"
                " WHERE ($1::text IS NULL OR s.display_name ILIKE '%' || $1 || '%')"
                " GROUP BY s.id, s.display_name, s.schoology_uid, s.role, s.school_id"
                " ORDER BY (count(c.id) = 0), s.display_name LIMIT $2",
                query,
                MAX_ROWS,
            )
            total = await db.fetchval(
                "SELECT count(*) FROM staff s"
                " WHERE ($1::text IS NULL OR s.display_name ILIKE '%' || $1 || '%')",
                query,
            )
        except Exception as exc:  # noqa: BLE001
            return _store_error(exc).payload()
        return await stamped(envelope([row(r) for r in rows], int(total), "staff"))

    @mcp.tool(
        annotations=_RO,
        name="school_get_sync_status",
        description=(
            "Per-source health of the Schoology sync: last run, status, "
            "records changed, and how stale the data is. Call this before "
            "telling anyone a child has nothing due or nothing missing — an "
            "empty result and a sync that broke a week ago look identical "
            "otherwise. `parsers_pending` counts page types the sync fetched "
            "but could not fully read."
        ),
    )
    async def get_sync_status() -> dict[str, Any]:
        try:
            fresh = await freshness()
            recent = await db.fetch(
                "SELECT source, started_at, finished_at, status, pages_fetched, records_seen,"
                " records_changed, parsers_pending, error FROM ingest_runs"
                " WHERE source <> 'seed' ORDER BY started_at DESC LIMIT 10"
            )
            archive = await db.fetchrow(
                "SELECT count(*) AS payloads, COALESCE(sum(byte_len), 0) AS bytes FROM raw_payloads"
            )
        except Exception as exc:  # noqa: BLE001
            return _store_error(exc).payload()
        return {
            **fresh,
            "recent_runs": [row(r) for r in recent],
            "raw_archive": row(archive) if archive else {},
            "note": "Synced roughly 7am and 4pm on weekdays. Nothing here is live.",
        }


def _parse_since(value: str | None, zone: ZoneInfo) -> datetime | None:
    """Parse an ISO date/datetime, or raise a ToolError naming the problem."""
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.strip())
    except ValueError as exc:
        raise ToolError(
            "bad_since",
            f"Could not read {value!r} as a date.",
            "Use an ISO date like 2026-09-01.",
        ) from exc
    # A bare date from the caller means local midnight, not UTC midnight.
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=zone)
