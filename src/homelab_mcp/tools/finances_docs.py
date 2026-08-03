"""The finances repo's governance docs, served to remote advisor sessions.

The numbers live in Actual; the *reasoning* lives in a git repo — PLAN.md's
targets and guardrails, DECISIONS.md's dated log of why, PULSE.md's contract
for what the weekly message says. A session that can read the ledger but not
the plan gives advice the household has already considered and rejected.

Three append tools write back, because a decision made in a session that never
reaches DECISIONS.md is a decision the next session will re-litigate, a want
that isn't frictionless to queue gets bought instead of queued, and something
that needs revisiting in November is otherwise trusted to memory.

TICKLERS.md is the one doc read as *data* rather than prose: the morning
sentinel asks it what has come due. Nothing here marks a tickler done — that
stays a deliberate human edit, so acknowledging a reminder costs a moment's
attention rather than happening as a side effect of being mentioned.

Deliberately narrow. There is no doc *editing* tool and no path that writes
PLAN.md: restructuring a governance document is session-with-git work where a
human sees the diff. These tools append, in one documented shape each, to
one file each.

Shelling out (AGENTS.md rule 3): every git invocation uses subprocess with
shell=False and a fully-built argv list. Doc names come from a fixed
allowlist, never from caller input, so no value can select a path. The token
is passed through the environment to a credential helper — never in argv,
where `ps` would show it.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import re
import shutil
import subprocess
import time
from collections.abc import Awaitable, Callable
from datetime import date, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Annotated, Any
from zoneinfo import ZoneInfo

from mcp.types import ToolAnnotations
from pydantic import Field

from homelab_mcp.tools._http import ToolError

if TYPE_CHECKING:
    from mcp.server.fastmcp import FastMCP

    from homelab_mcp.config import Settings

log = logging.getLogger(__name__)
audit = logging.getLogger("homelab_mcp.audit")

_RO = ToolAnnotations(
    readOnlyHint=True, destructiveHint=False, idempotentHint=True, openWorldHint=False
)
# Appends a new entry and pushes. Not destructive (nothing is overwritten),
# but each call adds another entry, so it is not repeat-safe.
_APPEND = ToolAnnotations(
    readOnlyHint=False, destructiveHint=False, idempotentHint=False, openWorldHint=False
)

# The exact set of documents this server will serve. A fixed allowlist rather
# than a directory listing: it keeps caller input out of path construction
# entirely, and stops an unrelated file that lands in the repo (an export, a
# scratch note) from becoming readable over the network by accident.
DOCS: tuple[str, ...] = (
    "PLAN.md",
    "DECISIONS.md",
    "PULSE.md",
    "REVIEW.md",
    "OPERATIONS.md",
    "PLANNED.md",
    "ARCHITECTURE.md",
    "TICKLERS.md",
)
RESOURCE_SCHEME = "finances"

DECISIONS_DOC = "DECISIONS.md"
PLANNED_DOC = "PLANNED.md"
TICKLERS_DOC = "TICKLERS.md"
LANES = ("WANT", "PROJECT", "MAINTENANCE")

# The household runs on Eastern time, and "is this due today?" must not depend
# on where the server happens to be. date.today() would silently answer in the
# host's zone — right on forge today, wrong the moment anything moves.
HOUSEHOLD_TZ = ZoneInfo("America/New_York")
TICKLER_ID_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,58}[a-z0-9]$")


def household_today() -> date:
    return datetime.now(HOUSEHOLD_TZ).date()


INSTRUCTIONS = """\
The finances repo's governance docs are available as `finances://` resources
(PLAN.md, DECISIONS.md, PULSE.md, REVIEW.md, OPERATIONS.md, PLANNED.md,
ARCHITECTURE.md, TICKLERS.md). Read PLAN.md before giving any financial advice
— it holds the targets, the guardrails and the decisions already made, and
advice that contradicts it has usually already been considered and rejected.

Content carries a `stale` flag when the local checkout could not be refreshed.
Say so rather than quoting a possibly-outdated figure as current.

`finances_decision_append`, `finances_planned_append` and
`finances_tickler_append` are append-only and push immediately. Use them so a
decision reached in conversation survives into the next session, so adding a
want stays frictionless, and so anything that needs revisiting on a date gets
scheduled instead of remembered.

`finances_ticklers` answers "is there anything to raise today?". An empty list
is the normal answer and needs no comment. Rows under `malformed` are reminders
that will NOT fire — surface them, because the failure is otherwise invisible
until the day it mattered. Nothing marks a tickler done: acknowledging one is a
deliberate edit to the file, which is what keeps the nag honest.
"""


# Resolved once, so the argv carries a full path rather than relying on PATH
# lookup at call time (ruff S607) — and so a PATH surprise fails loudly here
# rather than silently running something else.
_GIT = shutil.which("git") or "/run/current-system/sw/bin/git"

# Certificate-authority configuration varies by platform (Nix sets its own),
# and git cannot complete an HTTPS handshake without it.
_TLS_ENV = (
    "SSL_CERT_FILE",
    "SSL_CERT_DIR",
    "NIX_SSL_CERT_FILE",
    "GIT_SSL_CAINFO",
    "GIT_SSL_CAPATH",
    "CURL_CA_BUNDLE",
)


def _slug(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")[:60]


def _split_row(line: str) -> list[str]:
    """Cells of a markdown table row, honouring the `\\|` escape."""
    body = line.strip().strip("|")
    # Split on pipes that aren't backslash-escaped, then unescape.
    return [c.strip().replace(r"\|", "|") for c in re.split(r"(?<!\\)\|", body)]


def parse_ticklers(text: str, today: date) -> dict[str, Any]:
    """Parse TICKLERS.md's table into rows, malformed rows, and what's due.

    Every line that looks like a table row is accounted for in exactly one of
    `ticklers` or `malformed`. A tickler is a reminder someone scheduled for a
    future self; one that silently fails to parse is a reminder that will never
    fire, and the failure surfaces on the day it was needed and no earlier. So
    a row we cannot read is reported loudly rather than skipped.
    """
    rows: list[dict[str, Any]] = []
    malformed: list[dict[str, Any]] = []
    seen: dict[str, int] = {}

    for lineno, raw in enumerate(text.splitlines(), start=1):
        line = raw.strip()
        if not line.startswith("|"):
            continue
        cells = _split_row(line)
        # The header and its delimiter are structure, not data.
        if [c.lower() for c in cells[:4]] == ["id", "due", "status", "message"]:
            continue
        if all(set(c) <= {"-", ":"} and c for c in cells):
            continue

        def bad(reason: str, ln: int = lineno, text: str = line) -> None:
            malformed.append({"line": ln, "reason": reason, "raw": text[:300]})

        if len(cells) != 4:
            bad(f"expected 4 cells (id | due | status | message), found {len(cells)}")
            continue
        tid, due_raw, status_raw, message = cells
        if not tid:
            bad("empty id")
            continue
        try:
            due = date.fromisoformat(due_raw)
        except ValueError:
            bad(f"due date {due_raw!r} is not YYYY-MM-DD")
            continue
        status = status_raw.lower()
        if not status:
            bad("empty status")
            continue
        if not message:
            bad("empty message")
            continue
        # Duplicate ids are reported, not deduped: which one wins would be an
        # arbitrary choice, and both are still real reminders.
        if tid in seen:
            bad(f"duplicate id {tid!r} (also on line {seen[tid]})")
            continue
        seen[tid] = lineno
        rows.append(
            {
                "id": tid,
                "due": due.isoformat(),
                "status": status,
                "message": message,
                "line": lineno,
                "is_due": status == "open" and due <= today,
                "days_until_due": (due - today).days,
            }
        )

    return {
        "ticklers": rows,
        "malformed": malformed,
        "due": [r for r in rows if r["is_due"]],
    }


class Repo:
    """A local clone kept lazily fresh, with honest staleness reporting."""

    def __init__(self, path: Path, url: str, token: str, ttl_seconds: int) -> None:
        self.path = path
        self.url = url
        self.token = token
        self.ttl = ttl_seconds
        self._checked = 0.0
        self._stale_reason: str | None = None

    # ── git plumbing ──────────────────────────────────────────────────
    def _run(
        self, *args: str, check: bool = True, timeout: int = 60
    ) -> subprocess.CompletedProcess[str]:
        """Run one git command. shell=False, argv fully built here."""
        # Built from scratch rather than inheriting os.environ, so unrelated
        # secrets in the service's environment never reach a subprocess — but
        # the TLS trust store MUST be carried through. Omitting it made HTTPS
        # clones hang until the timeout with no useful error.
        env = {
            "GIT_TERMINAL_PROMPT": "0",  # never block waiting for a password
            # Isolate config EXPLICITLY rather than by repointing HOME. An
            # unexpected HOME made clones hang until the timeout with no
            # useful error; naming the config files is both deterministic and
            # debuggable, and it still ignores any user/system gitconfig that
            # might rewrite URLs or inject another credential helper.
            "GIT_CONFIG_GLOBAL": os.devnull,
            "GIT_CONFIG_SYSTEM": os.devnull,
            "PATH": os.environ.get("PATH", "/run/current-system/sw/bin:/usr/bin:/bin"),
            "GIT_AUTHOR_NAME": "homelab-mcp",
            "GIT_AUTHOR_EMAIL": "homelab-mcp@holthome.net",
            "GIT_COMMITTER_NAME": "homelab-mcp",
            "GIT_COMMITTER_EMAIL": "homelab-mcp@holthome.net",
        }
        for var in _TLS_ENV:
            if var in os.environ:
                env[var] = os.environ[var]
        argv = [_GIT]
        if self.token:
            # The helper reads the token from the environment, so it never
            # appears in argv where `ps` would expose it to any local user.
            env["HOMELAB_MCP_GIT_TOKEN"] = self.token
            argv += [
                "-c",
                "credential.helper=!f() { echo username=x-access-token; "
                'echo "password=$HOMELAB_MCP_GIT_TOKEN"; }; f',
            ]
        argv += list(args)
        return subprocess.run(  # noqa: S603 - shell=False, argv built above
            argv,
            cwd=str(self.path) if self.path.exists() else None,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=check,
            env=env,
        )

    def _clone(self) -> None:
        """Clone through the SAME credentialed path as every other command.

        Building a second argv here is how the initial clone ended up without
        the credential helper and prompted for a username against a private
        repo.
        """
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._run("clone", "--depth", "50", "--", self.url, str(self.path), timeout=180)

    def refresh(self, force: bool = False) -> str | None:
        """Pull if the cache has expired. Returns a staleness reason, or None.

        A pull failure is never an error to the caller: the last-known content
        is still the best answer available, and refusing to serve it would be
        worse than serving it labelled. What must never happen is serving it
        silently as current.
        """
        now = time.monotonic()
        if not force and self._checked and now - self._checked < self.ttl:
            return self._stale_reason
        try:
            if not (self.path / ".git").is_dir():
                self._clone()
            else:
                self._run("pull", "--ff-only", "--quiet")
            self._checked = now
            self._stale_reason = None
        except (subprocess.SubprocessError, OSError) as exc:
            detail = ""
            if isinstance(exc, subprocess.CalledProcessError):
                tail = (exc.stderr or "").strip().splitlines()
                detail = tail[-1] if tail else ""
            self._stale_reason = (
                f"Could not refresh the finances checkout ({exc.__class__.__name__}"
                f"{': ' + detail if detail else ''}). Serving the last cached copy; "
                "it may be out of date."
            )
            log.warning("finances repo refresh failed: %s", self._stale_reason)
            self._checked = now  # don't retry on every single call
        return self._stale_reason

    def head(self) -> str | None:
        try:
            return self._run("rev-parse", "--short", "HEAD").stdout.strip() or None
        except (subprocess.SubprocessError, OSError):
            return None

    # ── reads ─────────────────────────────────────────────────────────
    def read(self, name: str) -> tuple[str, str | None]:
        """(content, staleness_reason). `name` must already be allowlisted."""
        stale = self.refresh()
        target = self.path / name
        if not target.is_file():
            raise ToolError(
                "finances_doc_missing",
                f"{name} is not present in the checkout.",
                "It may not exist yet upstream, or the clone failed.",
            )
        return target.read_text(encoding="utf-8"), stale

    # ── appends ───────────────────────────────────────────────────────
    def commit_and_push(self, name: str, message: str) -> dict[str, Any]:
        try:
            self._run("add", "--", name)
            self._run("commit", "-m", message)
            self._run("push")
        except subprocess.CalledProcessError as exc:
            err = ((exc.stderr or "") + (exc.stdout or "")).strip()
            # Roll the working tree back so a failed push doesn't leave a
            # local-only commit that silently diverges from the remote.
            with contextlib.suppress(subprocess.SubprocessError, OSError):
                self._run("reset", "--hard", "origin/HEAD", check=False)
            hint = (
                "The configured token needs contents:write on the repo. A "
                "read-only token can serve the docs but cannot append to them."
                if "denied" in err.lower() or "403" in err or "authentication" in err.lower()
                else "Check the checkout's remote and network access."
            )
            raise ToolError("finances_push_failed", f"git failed: {err[:300]}", hint) from exc
        return {"head": self.head()}


def register(mcp: FastMCP, settings: Settings) -> None:
    """Register the finances_* doc resources and the two append tools."""
    repo = Repo(
        Path(settings.finances_repo_path),
        settings.finances_repo_url,
        settings.finances_repo_token,
        settings.finances_repo_ttl_seconds,
    )
    configured = bool(settings.finances_repo_path and settings.finances_repo_url)

    def _require() -> None:
        if not configured:
            raise ToolError(
                "finances_repo_not_configured",
                "The finances docs repo is not configured.",
                "Set HOMELAB_MCP_FINANCES_REPO_URL and _REPO_PATH.",
            )

    # ── resources ─────────────────────────────────────────────────────
    # One registration per document. The URI is built from the allowlist
    # constant, never from anything a caller supplies.
    def _make_reader(doc: str) -> Callable[[], Awaitable[str]]:
        async def _read() -> str:
            _require()
            content, stale = await asyncio.to_thread(repo.read, doc)
            if stale:
                # Prepended, not appended: a reader who stops early must still
                # see that the content might be out of date.
                return f"> **STALE:** {stale}\n\n{content}"
            return content

        return _read

    for doc in DOCS:
        mcp.resource(
            f"{RESOURCE_SCHEME}://{doc}",
            name=doc,
            description=f"{doc} from the household finances repo (governance doc).",
            mime_type="text/markdown",
        )(_make_reader(doc))

    # ── fallback reader, for clients awkward with resources ───────────
    @mcp.tool(
        annotations=_RO,
        name="finances_docs_get",
        description=(
            "Read one of the household finances governance documents: PLAN.md "
            "(targets, guardrails, open questions), DECISIONS.md (dated log of "
            "what was decided and why), PULSE.md, REVIEW.md, OPERATIONS.md, "
            "PLANNED.md (the spending queue) or ARCHITECTURE.md. Read PLAN.md "
            "before offering financial advice — advice that contradicts it has "
            "usually already been considered and rejected. The same content is "
            "available as `finances://` resources; this tool exists for clients "
            "that handle resources awkwardly. Content is flagged `stale` if the "
            "local checkout could not be refreshed."
        ),
    )
    async def docs_get(
        name: Annotated[str, Field(description=f"One of: {', '.join(DOCS)}")],
    ) -> dict[str, Any]:
        try:
            _require()
            if name not in DOCS:
                raise ToolError(
                    "finances_unknown_doc",
                    f"No document named {name!r}.",
                    "Available: " + ", ".join(DOCS),
                )
            content, stale = await asyncio.to_thread(repo.read, name)
        except ToolError as err:
            return err.payload()
        return {
            "name": name,
            "content": content,
            "stale": stale is not None,
            "stale_reason": stale,
            "revision": repo.head(),
        }

    # ── appends ───────────────────────────────────────────────────────
    @mcp.tool(
        annotations=_APPEND,
        name="finances_decision_append",
        description=(
            "Append a dated entry to the household DECISIONS.md log and push "
            "it. Use this whenever a session reaches a decision worth keeping — "
            "a decision that never lands in the log is one the next session "
            "will re-litigate from scratch. The entry goes at the top "
            "(newest-first) with today's date and the title you give it; the "
            "body should say WHAT was decided and WHY, because the why is what "
            "makes the log useful later. Append-only: it cannot edit or remove "
            "existing entries, and touches no other file."
        ),
    )
    async def decision_append(
        title: Annotated[str, Field(min_length=3, max_length=120, description="Short title.")],
        body: Annotated[str, Field(min_length=1, description="Markdown body: what, and why.")],
    ) -> dict[str, Any]:
        try:
            _require()
            clean_title = title.strip()
            if "\n" in clean_title:
                raise ToolError("finances_bad_title", "Title must be a single line.", "")
            clean_body = body.strip()
            # A body line starting '## ' would read as a separate decision
            # entry and silently corrupt the log's structure.
            offending = [ln for ln in clean_body.splitlines() if re.match(r"^##\s", ln)]
            if offending:
                raise ToolError(
                    "finances_bad_body",
                    "Body lines may not start with '## ' — that would create a "
                    "phantom decision entry.",
                    "Use '###' or bullets for sub-structure.",
                )
            stale = await asyncio.to_thread(repo.refresh, True)
            if stale:
                raise ToolError(
                    "finances_repo_stale",
                    "Refusing to append: " + stale,
                    "Appending onto a stale checkout risks a push conflict or a "
                    "lost entry. Resolve the checkout first.",
                )
            entry = f"## {date.today().isoformat()} — {clean_title}\n\n{clean_body}\n"
            path = repo.path / DECISIONS_DOC
            text = path.read_text(encoding="utf-8")
            # Newest-first: insert before the first existing entry, after the
            # file's header prose.
            match = re.search(r"^## ", text, flags=re.M)
            cut = match.start() if match else len(text)
            path.write_text(text[:cut] + entry + "\n" + text[cut:], encoding="utf-8")
            result = await asyncio.to_thread(
                repo.commit_and_push, DECISIONS_DOC, f"decision: {clean_title}"
            )
        except ToolError as err:
            return err.payload()
        audit.info("finances_decision_append title=%s head=%s", clean_title, result.get("head"))
        return {
            "appended": True,
            "document": DECISIONS_DOC,
            "date": date.today().isoformat(),
            "title": clean_title,
            "revision": result.get("head"),
            "anchor": f"#{date.today().isoformat()}-{_slug(clean_title)}",
        }

    # ── ticklers ──────────────────────────────────────────────────────
    @mcp.tool(
        annotations=_RO,
        name="finances_ticklers",
        description=(
            "Future-dated reminders from TICKLERS.md. Call this every morning: "
            "with due_only=true (the default) it returns only rows whose status "
            "is 'open' and whose due date has arrived in America/New_York — "
            "usually an empty list, which means there is nothing to say. A row "
            "keeps being returned every day until a human or an advisor session "
            "edits its status, and that repetition is intentional: the reminder "
            "was scheduled precisely so it would not be forgotten. Use "
            "due_only=false to review the whole schedule. Rows that could not "
            "be parsed come back under `malformed` — treat those as reminders "
            "that will not fire, and say so, because a tickler nobody can read "
            "is a promise silently broken. No tool marks a tickler done; that "
            "is a deliberate file edit by design."
        ),
    )
    async def ticklers(
        due_only: Annotated[
            bool,
            Field(description="Only rows that are open and due today or earlier."),
        ] = True,
    ) -> dict[str, Any]:
        try:
            _require()
            content, stale = await asyncio.to_thread(repo.read, TICKLERS_DOC)
        except ToolError as err:
            return err.payload()
        today = household_today()
        parsed = parse_ticklers(content, today)
        rows = parsed["due"] if due_only else parsed["ticklers"]
        return {
            "as_of": today.isoformat(),
            "timezone": "America/New_York",
            "due_only": due_only,
            "ticklers": rows,
            "due_count": len(parsed["due"]),
            "total_count": len(parsed["ticklers"]),
            # Always reported, whatever due_only says: a row that failed to
            # parse cannot be known to be un-due.
            "malformed": parsed["malformed"],
            "stale": stale is not None,
            "stale_reason": stale,
            "revision": repo.head(),
        }

    @mcp.tool(
        annotations=_APPEND,
        name="finances_tickler_append",
        description=(
            "Schedule a future-dated reminder in TICKLERS.md and push it. Use "
            "this whenever something needs revisiting on a date rather than "
            "now — a rate that reprices, a loan that rolls off, a token that "
            "expires, a decision deferred until a number is known. The row is "
            "created with status 'open'; from the due date onward the morning "
            "sentinel surfaces it daily until a human edits the status. The id "
            "must be unique, lowercase and hyphenated. Append-only: it cannot "
            "edit, complete or remove an existing tickler."
        ),
    )
    async def tickler_append(
        id: Annotated[  # noqa: A002 - matches the column name in TICKLERS.md
            str,
            Field(description="Unique lowercase-hyphenated key, e.g. 'usaa-auto-reprice'."),
        ],
        due: Annotated[str, Field(description="Date it should surface, 'YYYY-MM-DD'.")],
        message: Annotated[
            str,
            Field(min_length=3, description="What the future reader needs to know and do."),
        ],
    ) -> dict[str, Any]:
        try:
            _require()
            tid = id.strip().lower()
            if not TICKLER_ID_RE.match(tid):
                raise ToolError(
                    "finances_bad_tickler_id",
                    f"id {id!r} must be lowercase letters, digits and hyphens.",
                    "For example: 'usaa-auto-reprice'.",
                )
            try:
                due_date = date.fromisoformat(due.strip())
            except ValueError as exc:
                raise ToolError(
                    "finances_bad_due_date", f"due {due!r} must be 'YYYY-MM-DD'.", ""
                ) from exc
            clean_message = " ".join(message.strip().split())
            if not clean_message:
                raise ToolError("finances_bad_tickler", "message must not be empty.", "")

            stale = await asyncio.to_thread(repo.refresh, True)
            if stale:
                raise ToolError(
                    "finances_repo_stale",
                    "Refusing to append: " + stale,
                    "Appending onto a stale checkout risks a push conflict or a "
                    "lost reminder. Resolve the checkout first.",
                )

            path = repo.path / TICKLERS_DOC
            text = path.read_text(encoding="utf-8")
            existing = parse_ticklers(text, household_today())
            if any(r["id"] == tid for r in existing["ticklers"]):
                raise ToolError(
                    "finances_duplicate_tickler",
                    f"A tickler with id {tid!r} already exists.",
                    "Pick another id, or edit the existing row directly to reschedule it.",
                )

            lines = text.splitlines()
            try:
                header = next(
                    i
                    for i, ln in enumerate(lines)
                    if ln.startswith("|")
                    and [c.lower() for c in _split_row(ln)[:2]] == ["id", "due"]
                )
            except StopIteration as exc:
                raise ToolError(
                    "finances_table_not_found",
                    "Could not find the tickler table in TICKLERS.md.",
                    "Its header row must be '| id | due | status | message |'.",
                ) from exc
            end = header + 1
            while end + 1 < len(lines) and lines[end + 1].startswith("|"):
                end += 1
            # A literal pipe would split the cell and corrupt the table — the
            # parser would then report this very row as malformed.
            safe_message = clean_message.replace("|", r"\|")
            row = f"| {tid} | {due_date.isoformat()} | open | {safe_message} |"
            lines.insert(end + 1, row)
            path.write_text("\n".join(lines) + "\n", encoding="utf-8")
            result = await asyncio.to_thread(
                repo.commit_and_push, TICKLERS_DOC, f"tickler: {tid} ({due_date.isoformat()})"
            )
        except ToolError as err:
            return err.payload()
        audit.info("finances_tickler_append id=%s due=%s", tid, due_date.isoformat())
        return {
            "appended": True,
            "document": TICKLERS_DOC,
            "id": tid,
            "due": due_date.isoformat(),
            "status": "open",
            "message": clean_message,
            "row": row,
            "days_until_due": (due_date - household_today()).days,
            "revision": result.get("head"),
        }

    @mcp.tool(
        annotations=_APPEND,
        name="finances_planned_append",
        description=(
            "Add a row to PLANNED.md's spending queue and push it. Adding a "
            "want is meant to cost nothing — the queue is a promise, not a "
            "denial — so use this freely rather than talking someone out of an "
            "idea. Lane is WANT (guilt-free, funded from the bonus slice in "
            "queue order), PROJECT (needs an estimate and pre-committed "
            "dollars before work starts) or MAINTENANCE (safety/upkeep, "
            "scheduled promptly rather than queued). Give the estimate, the "
            "funding source and the gate/timing. Append-only; it cannot edit "
            "or reorder existing rows."
        ),
    )
    async def planned_append(
        item: Annotated[str, Field(min_length=2, max_length=120, description="What it is.")],
        lane: Annotated[str, Field(description="WANT, PROJECT or MAINTENANCE.")],
        estimate: Annotated[str, Field(description="Cost estimate, e.g. '$700' or 'TBD'.")],
        funding: Annotated[str, Field(description="Where the money comes from.")],
        gate: Annotated[str, Field(description="Gate or timing condition.")],
    ) -> dict[str, Any]:
        try:
            _require()
            lane_up = lane.strip().upper()
            if lane_up not in LANES:
                raise ToolError(
                    "finances_bad_lane",
                    f"Lane must be one of {', '.join(LANES)} (got {lane!r}).",
                    "",
                )
            cells = [item, lane_up, estimate, funding, gate]
            if any("\n" in c for c in cells):
                raise ToolError("finances_bad_row", "Row fields must be single-line.", "")
            # A literal pipe would split the cell and corrupt the table.
            safe = [c.strip().replace("|", r"\|") for c in cells]
            # Matches the existing rows' convention.
            status = (
                "open"
                if lane_up == "MAINTENANCE"
                else ("needs estimate" if not safe[2] or "tbd" in safe[2].lower() else "queued")
            )
            stale = await asyncio.to_thread(repo.refresh, True)
            if stale:
                raise ToolError("finances_repo_stale", "Refusing to append: " + stale, "")

            path = repo.path / PLANNED_DOC
            lines = path.read_text(encoding="utf-8").splitlines()
            # Find the queue table's last contiguous row.
            try:
                header = next(
                    i for i, ln in enumerate(lines) if ln.startswith("| Item") and "Lane" in ln
                )
            except StopIteration as exc:
                raise ToolError(
                    "finances_table_not_found",
                    "Could not find the queue table in PLANNED.md.",
                    "Its header row must start '| Item' and contain 'Lane'.",
                ) from exc
            end = header + 1
            while end + 1 < len(lines) and lines[end + 1].startswith("|"):
                end += 1
            row = "| " + " | ".join([*safe, status]) + " |"
            lines.insert(end + 1, row)
            path.write_text("\n".join(lines) + "\n", encoding="utf-8")
            result = await asyncio.to_thread(
                repo.commit_and_push, PLANNED_DOC, f"planned: {safe[0]}"
            )
        except ToolError as err:
            return err.payload()
        audit.info("finances_planned_append item=%s lane=%s", safe[0], lane_up)
        return {
            "appended": True,
            "document": PLANNED_DOC,
            "item": safe[0],
            "lane": lane_up,
            "status": status,
            "row": row,
            "revision": result.get("head"),
        }
