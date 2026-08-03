"""Governance-doc resource + append-tool tests.

These run against a real local git repo (a bare "remote" plus a clone), not a
mock: the append tools' whole job is to produce a commit that actually lands,
and a mocked git would prove nothing about that.
"""

from __future__ import annotations

import asyncio
import re
import subprocess
from collections.abc import Callable
from datetime import date
from pathlib import Path
from typing import Any

import pytest

from homelab_mcp.config import Settings
from homelab_mcp.tools.finances_docs import DOCS, Repo, parse_ticklers, register

DECISIONS = """# Decision Log

One dated entry per decision, with the why. Newest first.

## 2026-08-01 — An earlier decision

Body of the earlier one.
"""

PLANNED = """# Planned Spending — the queue

## Queue

| Item | Lane | Est. | Funding | Gate / timing | Status |
|---|---|---|---|---|---|
| Well water test | MAINTENANCE | $500 | Monthly cashflow | Within 60 days | open |

## Rules of the queue

1. Nothing here is bought on credit.
"""


TICKLERS = """# Ticklers — future-dated reminders the sentinel checks every morning

_Strict table format — the parser depends on it._

| id | due | status | message |
|---|---|---|---|
| buffer-floor | 2026-10-29 | open | October advance should have landed |
| usaa-auto-reprice | 2026-11-10 | open | USAA auto policy reprices ~now |
| kubota-sax-rolloff | 2026-11-20 | open | Kubota + sax financing should be done |
| irs-estimated-q4 | 2026-12-30 | open | Q4 estimated payment due mid-January |
| pat-renewal | 2027-07-01 | open | GitHub PAT expires ~Aug 2027 — renew early |
"""


def _git(*args: str, cwd: Path) -> None:
    subprocess.run(
        ["git", *args],
        cwd=cwd,
        check=True,
        capture_output=True,
        env={
            "GIT_AUTHOR_NAME": "t",
            "GIT_AUTHOR_EMAIL": "t@t",
            "GIT_COMMITTER_NAME": "t",
            "GIT_COMMITTER_EMAIL": "t@t",
            "PATH": "/usr/bin:/bin:/run/current-system/sw/bin",
            "HOME": str(cwd),
        },
    )


@pytest.fixture
def repo_pair(tmp_path: Path) -> tuple[Path, Path]:
    """A bare remote plus a working clone, so push/pull are real."""
    remote = tmp_path / "remote.git"
    work = tmp_path / "seed"
    work.mkdir()
    _git("init", "-q", "-b", "main", cwd=work)
    for name in DOCS:
        body = {
            "DECISIONS.md": DECISIONS,
            "PLANNED.md": PLANNED,
            "TICKLERS.md": TICKLERS,
        }.get(name, f"# {name}\n")
        (work / name).write_text(body)
    _git("add", ".", cwd=work)
    _git("commit", "-qm", "seed", cwd=work)
    subprocess.run(
        ["git", "clone", "-q", "--bare", str(work), str(remote)], check=True, capture_output=True
    )
    _git("remote", "add", "origin", str(remote), cwd=work)
    _git("push", "-q", "-u", "origin", "main", cwd=work)
    return remote, tmp_path / "checkout"


class CapturingMCP:
    def __init__(self) -> None:
        self.tools: dict[str, Callable[..., Any]] = {}
        self.resources: dict[str, Callable[..., Any]] = {}

    def tool(self, *, name: str, description: str = "", annotations: Any = None) -> Any:
        def deco(fn: Callable[..., Any]) -> Callable[..., Any]:
            self.tools[name] = fn
            return fn

        return deco

    def resource(self, uri: str, **kw: Any) -> Any:
        def deco(fn: Callable[..., Any]) -> Callable[..., Any]:
            self.resources[uri] = fn
            return fn

        return deco


def _mk(repo_pair: tuple[Path, Path]) -> CapturingMCP:
    remote, checkout = repo_pair
    mcp = CapturingMCP()
    register(
        mcp,  # type: ignore[arg-type]
        Settings(
            _env_file=None,  # type: ignore[call-arg]
            oauth_required=False,
            finances_repo_url=str(remote),
            finances_repo_path=str(checkout),
            finances_repo_ttl_seconds=0,
        ),
    )
    return mcp


# ── resources ─────────────────────────────────────────────────────────


def test_every_doc_is_registered_as_a_resource(repo_pair: tuple[Path, Path]) -> None:
    mcp = _mk(repo_pair)
    assert set(mcp.resources) == {f"finances://{d}" for d in DOCS}
    assert "finances://OPERATIONS.md" in mcp.resources
    assert "finances://PLANNED.md" in mcp.resources
    assert "finances://TICKLERS.md" in mcp.resources


async def test_resource_returns_current_content(repo_pair: tuple[Path, Path]) -> None:
    mcp = _mk(repo_pair)
    body = await mcp.resources["finances://DECISIONS.md"]()
    assert "An earlier decision" in body
    assert not body.startswith("> **STALE:**")


async def test_docs_get_rejects_a_name_outside_the_allowlist(
    repo_pair: tuple[Path, Path],
) -> None:
    """Caller input never reaches path construction."""
    mcp = _mk(repo_pair)
    for bad in ("../../etc/passwd", "README.md", ".env", "partner-brief.md"):
        out = await mcp.tools["finances_docs_get"](name=bad)
        assert out["error"]["code"] == "finances_unknown_doc"


async def test_unreachable_remote_degrades_with_a_staleness_warning(
    repo_pair: tuple[Path, Path],
) -> None:
    """A pull failure must serve cached content labelled, never an error."""
    remote, checkout = repo_pair
    mcp = _mk(repo_pair)
    await mcp.tools["finances_docs_get"](name="PLAN.md")  # populate the clone
    # Break the remote after the clone exists.
    remote.rename(remote.with_suffix(".gone"))

    out = await mcp.tools["finances_docs_get"](name="PLAN.md")
    assert out["stale"] is True
    assert "may be out of date" in out["stale_reason"]
    assert "# PLAN.md" in out["content"]  # cached content still served
    assert "error" not in out

    body = await mcp.resources["finances://PLAN.md"]()
    assert body.startswith("> **STALE:**")  # warning leads, not trails


# ── decision append ───────────────────────────────────────────────────


async def test_decision_append_lands_newest_first_and_pushes(
    repo_pair: tuple[Path, Path],
) -> None:
    remote, checkout = repo_pair
    mcp = _mk(repo_pair)
    out = await mcp.tools["finances_decision_append"](
        title="Buffer floor set to $15k", body="Agreed with partner.\n\nWhy: two months of fixed."
    )
    assert out["appended"] is True

    text = (checkout / "DECISIONS.md").read_text()
    # Header prose survives; the new entry precedes the older one.
    assert text.startswith("# Decision Log")
    assert text.index("Buffer floor set to $15k") < text.index("An earlier decision")
    assert "One dated entry per decision" in text

    # And it actually reached the remote. Off-thread: this test is async and
    # a blocking subprocess would stall the loop.
    log = (
        await asyncio.to_thread(
            subprocess.run,
            ["git", "log", "--oneline", "-1", "main"],
            cwd=remote,
            capture_output=True,
            text=True,
            check=True,
        )
    ).stdout
    assert "decision: Buffer floor set to $15k" in log


async def test_decision_append_rejects_a_body_that_would_fake_an_entry(
    repo_pair: tuple[Path, Path],
) -> None:
    """A '## ' line would silently split into a phantom decision."""
    mcp = _mk(repo_pair)
    out = await mcp.tools["finances_decision_append"](
        title="Fine", body="ok\n## 2020-01-01 — Injected\nbad"
    )
    assert out["error"]["code"] == "finances_bad_body"


async def test_decision_append_rejects_a_multiline_title(
    repo_pair: tuple[Path, Path],
) -> None:
    mcp = _mk(repo_pair)
    out = await mcp.tools["finances_decision_append"](title="a\nb", body="x")
    assert out["error"]["code"] == "finances_bad_title"


# ── planned append ────────────────────────────────────────────────────


async def test_planned_append_adds_a_table_row(repo_pair: tuple[Path, Path]) -> None:
    remote, checkout = repo_pair
    mcp = _mk(repo_pair)
    out = await mcp.tools["finances_planned_append"](
        item="Ooni Halo mixer",
        lane="want",  # case-insensitive
        estimate="$700",
        funding="Bonus guilt-free slice",
        gate="After cards hold $0 a full cycle",
    )
    assert out["lane"] == "WANT"
    assert out["status"] == "queued"

    lines = (checkout / "PLANNED.md").read_text().splitlines()
    table = [ln for ln in lines if ln.startswith("|")]
    # Header, separator, the seeded row, then ours — still contiguous.
    assert table[-1] == out["row"]
    assert "Ooni Halo mixer" in table[-1]
    # The section after the table is untouched.
    assert "## Rules of the queue" in "\n".join(lines)


async def test_planned_append_status_follows_the_lane(
    repo_pair: tuple[Path, Path],
) -> None:
    mcp = _mk(repo_pair)
    maint = await mcp.tools["finances_planned_append"](
        item="Gutter clean", lane="MAINTENANCE", estimate="$300", funding="Cashflow", gate="Now"
    )
    assert maint["status"] == "open"  # maintenance jumps the queue
    proj = await mcp.tools["finances_planned_append"](
        item="Berry garden", lane="PROJECT", estimate="TBD", funding="October advance", gate="Fall"
    )
    assert proj["status"] == "needs estimate"


async def test_planned_append_escapes_pipes(repo_pair: tuple[Path, Path]) -> None:
    """A literal pipe would split the cell and corrupt the table."""
    mcp = _mk(repo_pair)
    out = await mcp.tools["finances_planned_append"](
        item="A | B", lane="WANT", estimate="$1", funding="x", gate="y"
    )
    assert r"A \| B" in out["row"]
    # The property that matters: splitting on UNESCAPED pipes still yields
    # exactly six cells, so the row renders in the table it was added to.
    cells = re.split(r"(?<!\\)\|", out["row"])[1:-1]
    assert len(cells) == 6
    assert cells[0].strip() == r"A \| B"


async def test_planned_append_rejects_an_unknown_lane(
    repo_pair: tuple[Path, Path],
) -> None:
    mcp = _mk(repo_pair)
    out = await mcp.tools["finances_planned_append"](
        item="x", lane="SOMEDAY", estimate="$1", funding="y", gate="z"
    )
    assert out["error"]["code"] == "finances_bad_lane"


# ── safety ────────────────────────────────────────────────────────────


def test_token_never_appears_in_argv(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """`ps` must not expose the credential to any local user."""
    seen: list[list[str]] = []

    def fake_run(argv: list[str], **kw: Any) -> Any:
        seen.append(argv)
        env = kw.get("env") or {}
        assert "SECRET-TOKEN" not in " ".join(argv)
        assert env.get("HOMELAB_MCP_GIT_TOKEN") == "SECRET-TOKEN"
        return subprocess.CompletedProcess(argv, 0, "", "")

    monkeypatch.setattr(subprocess, "run", fake_run)
    repo = Repo(tmp_path / "c", "https://example.invalid/r.git", "SECRET-TOKEN", 0)
    (tmp_path / "c" / ".git").mkdir(parents=True)
    repo.refresh(force=True)
    assert seen, "expected a git invocation"


# ── ticklers ──────────────────────────────────────────────────────────

TODAY = date(2026, 8, 3)


def test_parser_reads_the_seeded_table() -> None:
    out = parse_ticklers(TICKLERS, TODAY)
    assert [r["id"] for r in out["ticklers"]] == [
        "buffer-floor",
        "usaa-auto-reprice",
        "kubota-sax-rolloff",
        "irs-estimated-q4",
        "pat-renewal",
    ]
    assert out["malformed"] == []
    # Earliest is 2026-10-29 — nothing is due yet.
    assert out["due"] == []
    assert out["ticklers"][0]["days_until_due"] == 87


def test_header_and_delimiter_are_not_data() -> None:
    """The '|---|' separator must not be mistaken for a broken row."""
    assert parse_ticklers(TICKLERS, TODAY)["malformed"] == []


@pytest.mark.parametrize(
    ("row", "reason_contains"),
    [
        ("| only-three | 2026-01-01 | open |", "found 3"),
        ("| too | many | 2026-01-01 | open | cells |", "found 5"),
        ("| bad-date | 29-10-2026 | open | message |", "not YYYY-MM-DD"),
        ("| bad-date | someday | open | message |", "not YYYY-MM-DD"),
        ("|  | 2026-01-01 | open | message |", "empty id"),
        ("| no-status | 2026-01-01 |  | message |", "empty status"),
        ("| no-message | 2026-01-01 | open |  |", "empty message"),
    ],
)
def test_malformed_rows_are_reported_never_dropped(row: str, reason_contains: str) -> None:
    """A tickler that fails to parse is a reminder that will never fire."""
    out = parse_ticklers(TICKLERS + row + "\n", TODAY)
    assert len(out["ticklers"]) == 5  # the good rows still parse
    assert len(out["malformed"]) == 1
    assert reason_contains in out["malformed"][0]["reason"]
    assert out["malformed"][0]["raw"].startswith("|")


def test_duplicate_ids_are_reported_rather_than_silently_deduped() -> None:
    out = parse_ticklers(TICKLERS + "| pat-renewal | 2027-01-01 | open | dupe |\n", TODAY)
    assert len(out["malformed"]) == 1
    assert "duplicate id" in out["malformed"][0]["reason"]


def test_due_is_open_and_dated_today_or_earlier() -> None:
    extra = (
        "| yesterday | 2026-08-02 | open | should surface |\n"
        "| today | 2026-08-03 | open | should surface |\n"
        "| tomorrow | 2026-08-04 | open | should not |\n"
        "| already-done | 2026-01-01 | done | closed, stays for history |\n"
        "| snoozed | 2026-01-01 | snoozed | not open |\n"
    )
    out = parse_ticklers(TICKLERS + extra, TODAY)
    assert [r["id"] for r in out["due"]] == ["yesterday", "today"]
    # Non-open rows are still parsed and returned in the full listing.
    assert {"already-done", "snoozed"} <= {r["id"] for r in out["ticklers"]}


def test_status_matching_is_case_insensitive() -> None:
    out = parse_ticklers(TICKLERS + "| shouty | 2026-01-01 | OPEN | yes |\n", TODAY)
    assert [r["id"] for r in out["due"]] == ["shouty"]


def test_escaped_pipe_survives_a_round_trip() -> None:
    out = parse_ticklers(TICKLERS + r"| piped | 2026-01-01 | open | a \| b |" + "\n", TODAY)
    assert out["malformed"] == []
    assert next(r for r in out["ticklers"] if r["id"] == "piped")["message"] == "a | b"


async def test_ticklers_tool_defaults_to_due_only(repo_pair: tuple[Path, Path]) -> None:
    out = await _mk(repo_pair).tools["finances_ticklers"]()
    assert out["due_only"] is True
    assert out["ticklers"] == []  # earliest is 2026-10-29
    assert out["total_count"] == 5  # but all five parsed
    assert out["due_count"] == 0
    assert out["timezone"] == "America/New_York"
    assert out["stale"] is False


async def test_ticklers_tool_full_listing(repo_pair: tuple[Path, Path]) -> None:
    out = await _mk(repo_pair).tools["finances_ticklers"](due_only=False)
    assert len(out["ticklers"]) == 5
    assert out["ticklers"][0]["id"] == "buffer-floor"


async def test_malformed_surfaces_even_when_due_only(repo_pair: tuple[Path, Path]) -> None:
    """A row we cannot read cannot be known to be un-due."""
    remote, checkout = repo_pair
    mcp = _mk(repo_pair)
    await mcp.tools["finances_ticklers"]()  # clone
    path = checkout / "TICKLERS.md"
    path.write_text(path.read_text() + "| broken | not-a-date | open | oh no |\n")
    out = await mcp.tools["finances_ticklers"]()
    assert out["ticklers"] == []
    assert len(out["malformed"]) == 1
    assert "not YYYY-MM-DD" in out["malformed"][0]["reason"]


# ── tickler_append ────────────────────────────────────────────────────


async def test_tickler_append_round_trips_to_the_remote(
    repo_pair: tuple[Path, Path],
) -> None:
    remote, checkout = repo_pair
    mcp = _mk(repo_pair)
    out = await mcp.tools["finances_tickler_append"](
        id="new-thing", due="2026-09-30", message="check the thing"
    )
    assert out["appended"] is True
    assert out["status"] == "open"
    assert out["row"] == "| new-thing | 2026-09-30 | open | check the thing |"

    # It is really on the remote, not just locally. Off-thread: this test is
    # async and a blocking subprocess would stall the loop.
    landed = (
        await asyncio.to_thread(
            subprocess.run,
            ["git", "show", "HEAD:TICKLERS.md"],
            cwd=remote,
            capture_output=True,
            text=True,
            check=True,
        )
    ).stdout
    assert "| new-thing | 2026-09-30 | open | check the thing |" in landed
    # And it parses back out as a real tickler.
    back = parse_ticklers(landed, date(2026, 10, 1))
    assert next(r for r in back["ticklers"] if r["id"] == "new-thing")["is_due"] is True


async def test_tickler_append_lands_inside_the_table(repo_pair: tuple[Path, Path]) -> None:
    mcp = _mk(repo_pair)
    await mcp.tools["finances_tickler_append"](id="appended", due="2027-01-01", message="last row")
    out = await mcp.tools["finances_ticklers"](due_only=False)
    assert [r["id"] for r in out["ticklers"]][-1] == "appended"
    assert out["malformed"] == []


async def test_tickler_append_rejects_a_duplicate_id(repo_pair: tuple[Path, Path]) -> None:
    mcp = _mk(repo_pair)
    out = await mcp.tools["finances_tickler_append"](
        id="pat-renewal", due="2027-01-01", message="dupe"
    )
    assert out["error"]["code"] == "finances_duplicate_tickler"


@pytest.mark.parametrize(
    "bad_id", ["Has Caps", "under_score", "trailing-", "-leading", "has space", ""]
)
async def test_tickler_append_rejects_bad_ids(repo_pair: tuple[Path, Path], bad_id: str) -> None:
    mcp = _mk(repo_pair)
    out = await mcp.tools["finances_tickler_append"](id=bad_id, due="2027-01-01", message="x")
    assert out["error"]["code"] == "finances_bad_tickler_id"


async def test_tickler_append_rejects_a_bad_due_date(repo_pair: tuple[Path, Path]) -> None:
    mcp = _mk(repo_pair)
    out = await mcp.tools["finances_tickler_append"](id="whenever", due="next tuesday", message="x")
    assert out["error"]["code"] == "finances_bad_due_date"


async def test_tickler_append_escapes_a_pipe_rather_than_corrupting_the_table(
    repo_pair: tuple[Path, Path],
) -> None:
    mcp = _mk(repo_pair)
    await mcp.tools["finances_tickler_append"](
        id="pipey", due="2027-01-01", message="rate is 5|6 percent"
    )
    out = await mcp.tools["finances_ticklers"](due_only=False)
    assert out["malformed"] == []
    assert next(r for r in out["ticklers"] if r["id"] == "pipey")["message"] == (
        "rate is 5|6 percent"
    )


async def test_tickler_append_refuses_on_a_stale_checkout(
    repo_pair: tuple[Path, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Appending onto a stale checkout risks a lost reminder."""
    mcp = _mk(repo_pair)
    await mcp.tools["finances_ticklers"]()  # clone first
    monkeypatch.setattr(Repo, "refresh", lambda self, force=False: "remote unreachable")
    out = await mcp.tools["finances_tickler_append"](id="doomed", due="2027-01-01", message="x")
    assert out["error"]["code"] == "finances_repo_stale"


async def test_no_tool_can_mark_a_tickler_done(repo_pair: tuple[Path, Path]) -> None:
    """Acknowledgement is meant to cost one deliberate human edit."""
    names = set(_mk(repo_pair).tools)
    assert names & {"finances_ticklers", "finances_tickler_append"}
    for forbidden in (
        "finances_tickler_done",
        "finances_tickler_update",
        "finances_tickler_snooze",
        "finances_tickler_delete",
    ):
        assert forbidden not in names
