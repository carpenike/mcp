"""School-tool tests.

No database and no network: a fake reader stands in for the schoolhouse
Postgres pool, so these exercise what this module actually owns — the
projection logic, local-time rendering, the truncation envelope, child
scoping, and the structured-error contract.

The SQL itself is covered upstream in the schoolhouse repo's integration
suite, which runs against a real Postgres. Duplicating that here would need
a database in CI, which AGENTS.md rules out.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any
from zoneinfo import ZoneInfo

import pytest

from homelab_mcp.config import Settings
from homelab_mcp.tools import school
from homelab_mcp.tools.school import envelope, register, render_row

DSN = "postgresql://reader@localhost/schoolhouse"


class CapturingMCP:
    """Collects tools registered via @mcp.tool(name=...) so tests can call them."""

    def __init__(self) -> None:
        self.tools: dict[str, Callable[..., Any]] = {}

    def tool(
        self, *, name: str, description: str = "", annotations: Any = None
    ) -> Callable[..., Any]:
        def deco(fn: Callable[..., Any]) -> Callable[..., Any]:
            self.tools[name] = fn
            return fn

        return deco


class FakeReader:
    """Stands in for the asyncpg pool, returning canned rows per query."""

    def __init__(self, rows: list[dict[str, Any]] | None = None, scalar: Any = 0) -> None:
        self.rows = rows if rows is not None else []
        self.scalar = scalar
        self.queries: list[str] = []
        self.raises: Exception | None = None

    async def fetch(self, query: str, *args: Any) -> list[dict[str, Any]]:
        if self.raises:
            raise self.raises
        self.queries.append(query)
        # The freshness probe runs on every response; give it one clean run.
        if "FROM ingest_runs" in query:
            return [
                {
                    "source": "scraper",
                    "finished_at": datetime.now(UTC) - timedelta(hours=2),
                    "status": "ok",
                    "records_changed": 3,
                    "parsers_pending": 0,
                    "error": None,
                }
            ]
        if "FROM children WHERE active" in query:
            return [
                {"id": 1, "schoology_user_id": "1001", "display_name": "Student A"},
                {"id": 2, "schoology_user_id": "1002", "display_name": "Student B"},
            ]
        return self.rows

    async def fetchrow(self, query: str, *args: Any) -> dict[str, Any] | None:
        if self.raises:
            raise self.raises
        self.queries.append(query)
        return self.rows[0] if self.rows else None

    async def fetchval(self, query: str, *args: Any) -> Any:
        if self.raises:
            raise self.raises
        self.queries.append(query)
        return self.scalar


def build(
    monkeypatch: pytest.MonkeyPatch, reader: FakeReader, dsn: str = DSN
) -> dict[str, Callable[..., Any]]:
    """Register the category against a fake reader."""
    monkeypatch.setenv("HOMELAB_MCP_OAUTH_REQUIRED", "false")
    monkeypatch.setenv("HOMELAB_MCP_SCHOOLHOUSE_DATABASE_URL", dsn)
    monkeypatch.setattr(school, "_Reader", lambda _dsn: reader)
    mcp = CapturingMCP()
    register(mcp, Settings())  # type: ignore[arg-type]
    return mcp.tools


# ── registration ─────────────────────────────────────────────────────


def test_category_does_not_register_without_a_database(monkeypatch: pytest.MonkeyPatch) -> None:
    """An unconfigured upstream means no tools, not a broken server."""
    monkeypatch.setenv("HOMELAB_MCP_OAUTH_REQUIRED", "false")
    monkeypatch.setenv("HOMELAB_MCP_SCHOOLHOUSE_DATABASE_URL", "")
    mcp = CapturingMCP()
    register(mcp, Settings())  # type: ignore[arg-type]
    assert mcp.tools == {}


def test_every_tool_is_registered_and_read_only(monkeypatch: pytest.MonkeyPatch) -> None:
    tools = build(monkeypatch, FakeReader())
    assert set(tools) == {
        "school_list_children",
        "school_list_courses",
        "school_get_upcoming_work",
        "school_get_missing_work",
        "school_get_grades",
        "school_get_assignment",
        "school_get_announcements",
        "school_list_staff",
        "school_get_sync_status",
    }


# ── rendering ────────────────────────────────────────────────────────


def test_timestamps_render_in_the_configured_zone() -> None:
    """Storage is UTC; answers are local, or "due Thursday" is wrong at the edges."""
    due = datetime(2026, 9, 16, 3, 59, tzinfo=UTC)  # 11:59pm Eastern on the 15th
    out = render_row({"due_at": due}, ZoneInfo("America/New_York"))
    assert out["due_at"].startswith("2026-09-15T23:59:00-04:00")


def test_decimals_become_floats_and_other_values_pass_through() -> None:
    out = render_row({"pct": Decimal("92.500"), "title": "Essay", "n": None}, ZoneInfo("UTC"))
    assert out == {"pct": 92.5, "title": "Essay", "n": None}


def test_unknown_zone_falls_back_to_utc() -> None:
    assert str(school._load_zone("Not/AZone")) == "UTC"


def test_envelope_flags_truncation() -> None:
    assert envelope([{"a": 1}], total=57, key="courses")["truncated"] is True
    assert envelope([{"a": 1}], total=1, key="courses")["truncated"] is False


# ── behaviour ────────────────────────────────────────────────────────


async def test_responses_carry_freshness(monkeypatch: pytest.MonkeyPatch) -> None:
    tools = build(monkeypatch, FakeReader(rows=[{"display_name": "Student A"}], scalar=1))
    result = await tools["school_list_children"]()
    assert result["stale"] is False
    assert result["data_as_of"] is not None
    assert result["returned"] == 1


async def test_stale_data_is_flagged(monkeypatch: pytest.MonkeyPatch) -> None:
    """A sync that died two days ago must not read as current."""

    class StaleReader(FakeReader):
        async def fetch(self, query: str, *args: Any) -> list[dict[str, Any]]:
            if "FROM ingest_runs" in query:
                return [
                    {
                        "source": "scraper",
                        "finished_at": datetime.now(UTC) - timedelta(days=2),
                        "status": "ok",
                        "records_changed": 0,
                        "parsers_pending": 0,
                        "error": None,
                    }
                ]
            return await super().fetch(query, *args)

    tools = build(monkeypatch, StaleReader(rows=[]))
    assert (await tools["school_list_children"]())["stale"] is True


async def test_unknown_child_is_a_structured_error(monkeypatch: pytest.MonkeyPatch) -> None:
    tools = build(monkeypatch, FakeReader())
    result = await tools["school_list_courses"](child="Nobody")
    assert result["error"]["code"] == "unknown_child"
    assert "Student A" in result["error"]["hint"]


async def test_child_scoping_reaches_sql_not_the_model(monkeypatch: pytest.MonkeyPatch) -> None:
    reader = FakeReader(rows=[], scalar=0)
    tools = build(monkeypatch, reader)
    await tools["school_list_courses"](child="Student A")
    assert any("child_id = ANY($1)" in q for q in reader.queries)


async def test_missing_work_separates_observed_from_inferred(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    reader = FakeReader(
        rows=[
            {"title": "Essay", "basis": "observed", "days_past_due": 3},
            {"title": "Lab", "basis": "inferred_past_due", "days_past_due": 1},
        ]
    )
    tools = build(monkeypatch, reader)
    result = await tools["school_get_missing_work"]()
    assert (result["observed"], result["inferred"]) == (1, 1)


async def test_bad_assignment_id_is_rejected_before_the_query(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    reader = FakeReader()
    tools = build(monkeypatch, reader)
    result = await tools["school_get_assignment"](assignment_id="../../etc")
    assert result["error"]["code"] == "bad_assignment_id"
    assert reader.queries == []


async def test_store_failure_returns_the_error_contract(monkeypatch: pytest.MonkeyPatch) -> None:
    """Never raise to the transport, even when Postgres is down."""
    reader = FakeReader()
    reader.raises = OSError("connection refused")
    tools = build(monkeypatch, reader)
    result = await tools["school_list_children"]()
    assert result["error"]["code"] == "schoolhouse_unreachable"
    assert "OSError" in result["error"]["message"]


async def test_since_is_interpreted_in_local_time(monkeypatch: pytest.MonkeyPatch) -> None:
    reader = FakeReader(rows=[])
    tools = build(monkeypatch, reader)
    result = await tools["school_get_grades"](since="2026-09-01")
    assert result["series"] is True
    assert any("observed_at >= $3" in q for q in reader.queries)


async def test_bad_since_is_a_structured_error(monkeypatch: pytest.MonkeyPatch) -> None:
    tools = build(monkeypatch, FakeReader())
    result = await tools["school_get_grades"](since="last tuesday")
    assert result["error"]["code"] == "bad_since"
