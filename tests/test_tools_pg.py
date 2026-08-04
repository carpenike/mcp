"""Shared Postgres plumbing for store-backed tool categories.

`school_*` exercises most of this transitively, but three behaviours are
deliberate enough to pin directly: the envelope never lies about truncation,
the timezone fallback is loud rather than silent, and a storage failure never
carries the DSN into a client-visible payload.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from decimal import Decimal
from zoneinfo import ZoneInfo

from homelab_mcp.tools._pg import Reader, envelope, load_zone, render_row, store_error


class TestEnvelope:
    def test_full_result_is_not_truncated(self) -> None:
        out = envelope([{"a": 1}, {"a": 2}], total=2, key="items")
        assert out == {"items": [{"a": 1}, {"a": 2}], "returned": 2, "total": 2, "truncated": False}

    def test_short_result_says_so(self) -> None:
        # The whole point: a model given a silently truncated list answers as
        # though it saw everything.
        out = envelope([{"a": 1}], total=9, key="items")
        assert out["truncated"] is True
        assert out["returned"] == 1
        assert out["total"] == 9

    def test_empty_is_not_truncated(self) -> None:
        assert envelope([], total=0, key="items")["truncated"] is False


class TestRenderRow:
    def test_timestamps_render_in_the_display_zone(self) -> None:
        tz = ZoneInfo("America/New_York")
        row = {"due_at": datetime(2026, 9, 15, 23, 59, tzinfo=UTC)}
        out = render_row(row, tz)
        # 23:59Z is 7:59pm Eastern — rendering UTC would put this on the
        # wrong day for anyone asking "what is due Thursday".
        assert out["due_at"].startswith("2026-09-15T19:59")

    def test_decimal_becomes_float(self) -> None:
        out = render_row({"pct": Decimal("93.5")}, ZoneInfo("UTC"))
        assert out["pct"] == 93.5
        assert isinstance(out["pct"], float)

    def test_other_types_pass_through(self) -> None:
        out = render_row({"n": 5, "s": "x", "b": True, "none": None}, ZoneInfo("UTC"))
        assert out == {"n": 5, "s": "x", "b": True, "none": None}


class TestLoadZone:
    def test_resolves_a_real_zone(self) -> None:
        assert load_zone("America/New_York", label="lading").key == "America/New_York"

    def test_unknown_zone_falls_back_to_utc_loudly(self, caplog) -> None:
        with caplog.at_level(logging.ERROR):
            zone = load_zone("Mars/Olympus_Mons", label="lading")
        assert zone.key == "UTC"
        # The label is in the message so an operator knows which setting to
        # fix without grepping for the string.
        assert "lading" in caplog.text


class TestStoreError:
    def test_reports_the_class_and_never_the_message(self) -> None:
        # asyncpg failure text can quote the DSN, and the DSN carries a
        # password. Only the exception class may reach a client payload.
        exc = ConnectionError("could not connect to postgresql://user:hunter2@host/db")
        err = store_error(exc, code="lading_unreachable", store="lading", env_var="LADING_DSN")
        payload = err.payload()["error"]
        assert payload["code"] == "lading_unreachable"
        assert "ConnectionError" in payload["message"]
        assert "hunter2" not in str(payload)
        assert "postgresql://" not in str(payload)

    def test_hint_names_the_setting_to_check(self) -> None:
        err = store_error(
            RuntimeError("x"), code="c", store="lading", env_var="HOMELAB_MCP_AMAZON_DATABASE_URL"
        )
        assert "HOMELAB_MCP_AMAZON_DATABASE_URL" in err.payload()["error"]["hint"]


def test_reader_does_not_expose_a_write_path() -> None:
    """Read-only by construction, not by convention.

    The role this connects as holds `readonly` membership, but the absence of
    an execute() is the thing a reviewer can see without checking Postgres.
    """
    assert not hasattr(Reader("postgresql://unused"), "execute")
    assert sorted(m for m in vars(Reader) if not m.startswith("__")) == [
        "_ensure",
        "fetch",
        "fetchrow",
        "fetchval",
    ]
