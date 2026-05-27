"""RepLog tools — HOF-004 / ADR 016 / ADR 015.

Bridges Claude (mobile + desktop, via the homelab-mcp OAuth AS) into
RepLog (replog.holthome.net) as a session companion. The tool catalog
is deliberately tiered per HOF-004:

  Group A — reads (direct):
    replog_get_dashboard, replog_get_athlete, replog_list_workouts,
    replog_get_workout, replog_get_prescription, replog_get_tm_history,
    replog_get_training_maxes, replog_list_journal,
    replog_list_athlete_programs, replog_list_athlete_equipment.

  Group B — clerical writes (direct, gated by CanManageAthlete on the
  RepLog side):
    replog_create_workout, replog_log_set, replog_update_set,
    replog_delete_set, replog_update_workout_notes,
    replog_log_body_weight, replog_add_athlete_note.

  Group C — coaching changes (gated through the human approval gate):
    replog_enqueue_program_generation, replog_get_generation_status.

DELIBERATELY OMITTED tools (the absence IS the doctrine — ADR 007 /
HOF-004 [forbidden]):
  - No replog_execute_generation. Commit happens on the webui where
    the human's click is the approval.
  - No replog_create_training_max, no replog_assign_program, no
    replog_promote_athlete, no replog_apply_tm_bumps. These are
    coaching decisions, not clerical work; they live webui-only.

Auth: each tool reads the caller's `email` claim from the JWT stashed
on the ASGI scope by JWTAuthMiddleware, then re-mints a short-TTL
(default 60s) RS256 JWT addressed to `aud=https://replog.holthome.net`
via the per-process mint_token closure. The downstream replog binary
verifies the JWT against the JWKS at `<homelab-mcp-issuer>/oauth/jwks.json`,
resolves `email` → *models.User, checks `users.mcp_enabled`, and runs
the same access-control helpers (CanAccessAthlete / CanManageAthlete)
the webui uses. End-to-end identity, no shared service account, no
per-user bearer-token map anywhere.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Annotated, Any

import httpx
from mcp.server.fastmcp import Context
from pydantic import Field

if TYPE_CHECKING:
    from mcp.server.fastmcp import FastMCP

    from homelab_mcp.config import Settings
    from homelab_mcp.tools._registry import MintTokenFn

log = logging.getLogger(__name__)

# Outbound timeout for replog REST calls. Generous because the LLM
# generation enqueue path returns 202 quickly but other tools may
# block briefly on SQLite write contention; 30s is the practical
# ceiling for any interactive tool call.
_HTTP_TIMEOUT = 30.0

# Tool-hop JWT lifetime override. Mirrors settings.replog_tool_hop_ttl_seconds
# at call time so the registration captures the configured value, not a
# stale module-load snapshot.


def register(mcp: FastMCP, settings: Settings, mint_token: MintTokenFn) -> None:
    """Register replog_* tools on the given MCP server.

    No-op (with a warning) if `replog_base_url` is empty — that's how
    deployments without RepLog disable the integration without code
    changes.
    """
    base_url = (settings.replog_base_url or "").rstrip("/")
    if not base_url:
        log.warning(
            "replog tools NOT registered: HOMELAB_MCP_REPLOG_BASE_URL is empty. "
            "Set it to enable the RepLog integration (e.g. http://127.0.0.1:5008 "
            "on forge). See HOF-004."
        )
        return

    audience = settings.replog_audience
    ttl = settings.replog_tool_hop_ttl_seconds

    log.info(
        "replog tools registering: base=%s aud=%s ttl=%ds",
        base_url,
        audience,
        ttl,
    )

    # ── Identity helpers ────────────────────────────────────────────

    def _caller_identity(ctx: Context[Any, Any, Any]) -> tuple[str, str] | None:
        """Return (sub, email) from the inbound JWT, or None if missing.

        JWTAuthMiddleware stashes the decoded claims at scope["user"];
        FastMCP's Context.request_context.request exposes the Starlette
        Request whose .scope we can walk. Returning None lets the caller
        produce a clean tool-level error rather than raising into the
        transport (which would surface as a 500 to Claude with no useful
        explanation).
        """
        request = getattr(ctx.request_context, "request", None)
        if request is None:
            return None
        scope_user = request.scope.get("user") or {}
        if not isinstance(scope_user, dict):
            return None
        claims = scope_user.get("claims") or {}
        sub = claims.get("sub") or scope_user.get("sub")
        email = claims.get("email") or scope_user.get("email")
        if not isinstance(sub, str) or not isinstance(email, str):
            return None
        if not sub or not email:
            return None
        return sub, email

    async def _call_replog(
        ctx: Context[Any, Any, Any],
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        json: Any = None,
    ) -> dict[str, Any]:
        """Mint a tool-hop JWT and forward the request to replog's /api-mcp/*.

        Returns the JSON body on 2xx, or a tool-friendly error dict on
        any failure. Never raises into FastMCP — Claude reads the error
        dict and adapts.
        """
        identity = _caller_identity(ctx)
        if identity is None:
            return {
                "error": "missing identity",
                "reason": (
                    "The MCP transport did not provide a verified caller "
                    "identity. This usually means OAuth is disabled or the "
                    "inbound JWT was missing the email claim."
                ),
            }
        sub, email = identity

        try:
            token = mint_token(
                sub=sub,
                email=email,
                audience=audience,
                ttl_seconds=ttl,
            )
        except Exception as e:
            log.exception("replog: tool-hop mint failed for %s", email)
            return {"error": "token mint failed", "reason": str(e)}

        url = base_url + path
        headers = {"Authorization": f"Bearer {token}"}

        try:
            async with httpx.AsyncClient(timeout=_HTTP_TIMEOUT) as client:
                resp = await client.request(
                    method,
                    url,
                    params=params,
                    json=json,
                    headers=headers,
                )
        except httpx.RequestError as e:
            log.warning("replog: transport error to %s: %s", url, e)
            return {
                "error": "replog unreachable",
                "reason": f"could not contact RepLog at {base_url}: {e}",
            }

        if 200 <= resp.status_code < 300:
            try:
                body: Any = resp.json()
            except ValueError:
                return {
                    "error": "replog returned non-JSON success",
                    "status_code": resp.status_code,
                    "raw": resp.text[:500],
                }
            if not isinstance(body, dict):
                # Wrap arrays so the MCP tool return type stays dict[str, Any].
                return {"results": body}
            return body

        # Surface replog's structured error body verbatim when available
        # — it carries `reason` slugs (mcp-not-enabled, missing-email-claim,
        # access denied, etc.) the agent can pattern-match on.
        try:
            err: Any = resp.json()
        except ValueError:
            err = {"error": resp.text[:500]}
        if not isinstance(err, dict):
            err = {"error": str(err)[:500]}
        err.setdefault("status_code", resp.status_code)
        log.info(
            "replog: %s %s → %d (reason=%s)",
            method,
            url,
            resp.status_code,
            err.get("reason"),
        )
        typed_err: dict[str, Any] = err
        return typed_err

    # ── Group A: reads ──────────────────────────────────────────────

    @mcp.tool(
        name="replog_get_dashboard",
        description=(
            "Get the RepLog dashboard for the calling coach. Lists "
            "manageable athletes plus quick-glance stats (active "
            "assignments, last workout, week streak, body-weight trend). "
            "Use this as the entry point when the user asks about "
            "RepLog without naming a specific athlete."
        ),
    )
    async def get_dashboard(ctx: Context[Any, Any, Any]) -> dict[str, Any]:
        return await _call_replog(ctx, "GET", "/api-mcp/dashboard")

    @mcp.tool(
        name="replog_get_athlete",
        description=(
            "Get one athlete's profile — name, tier, goal, notes, "
            "demographics. Combine with replog_get_prescription / "
            "replog_list_workouts / replog_get_tm_history for the full "
            "training context."
        ),
    )
    async def get_athlete(
        ctx: Context[Any, Any, Any],
        athlete_id: Annotated[int, Field(ge=1, description="RepLog athlete ID")],
    ) -> dict[str, Any]:
        return await _call_replog(ctx, "GET", f"/api-mcp/athletes/{athlete_id}")

    @mcp.tool(
        name="replog_list_workouts",
        description=(
            "List recent workouts for one athlete (most recent first). "
            "Each entry includes date, total set count, review status, "
            "and the assigned program name. Use replog_get_workout to "
            "pull the full set detail."
        ),
    )
    async def list_workouts(
        ctx: Context[Any, Any, Any],
        athlete_id: Annotated[int, Field(ge=1)],
    ) -> dict[str, Any]:
        return await _call_replog(ctx, "GET", f"/api-mcp/athletes/{athlete_id}/workouts")

    @mcp.tool(
        name="replog_get_workout",
        description=(
            "Get one workout's full detail including every logged set "
            "(exercise, weight, reps, RPE, notes). Use after "
            "replog_list_workouts narrows the date you care about."
        ),
    )
    async def get_workout(
        ctx: Context[Any, Any, Any],
        athlete_id: Annotated[int, Field(ge=1)],
        workout_id: Annotated[int, Field(ge=1, description="Workout ID from replog_list_workouts")],
    ) -> dict[str, Any]:
        return await _call_replog(
            ctx,
            "GET",
            f"/api-mcp/athletes/{athlete_id}/workouts/{workout_id}",
        )

    @mcp.tool(
        name="replog_get_prescription",
        description=(
            "Get the next prescribed work for an athlete today — the "
            "exercises, sets, reps, and target weights from their active "
            "program. Use this when the user is in the gym about to start "
            "a session ('what should I do today?'). Combines training "
            "maxes + program template + recent workout history."
        ),
    )
    async def get_prescription(
        ctx: Context[Any, Any, Any],
        athlete_id: Annotated[int, Field(ge=1)],
    ) -> dict[str, Any]:
        return await _call_replog(ctx, "GET", f"/api-mcp/athletes/{athlete_id}/prescription")

    @mcp.tool(
        name="replog_get_training_maxes",
        description=(
            "List all current training maxes for one athlete (most recent "
            "value per exercise). Use replog_get_tm_history for the full "
            "time series on a single lift."
        ),
    )
    async def get_training_maxes(
        ctx: Context[Any, Any, Any],
        athlete_id: Annotated[int, Field(ge=1)],
    ) -> dict[str, Any]:
        return await _call_replog(ctx, "GET", f"/api-mcp/athletes/{athlete_id}/training-maxes")

    @mcp.tool(
        name="replog_get_tm_history",
        description=(
            "Full training-max history for one athlete + one exercise, "
            "oldest to newest. Use when the user asks 'how has my squat "
            "TM moved over time' or for cycle-over-cycle progression "
            "review."
        ),
    )
    async def get_tm_history(
        ctx: Context[Any, Any, Any],
        athlete_id: Annotated[int, Field(ge=1)],
        exercise_id: Annotated[int, Field(ge=1, description="Exercise ID")],
    ) -> dict[str, Any]:
        return await _call_replog(
            ctx,
            "GET",
            f"/api-mcp/athletes/{athlete_id}/exercises/{exercise_id}/training-maxes",
        )

    @mcp.tool(
        name="replog_list_journal",
        description=(
            "List the athlete's journal — coach notes + workout reviews + "
            "athlete-authored entries — most recent first. Useful context "
            "for understanding what the coach has been thinking before "
            "drafting a program."
        ),
    )
    async def list_journal(
        ctx: Context[Any, Any, Any],
        athlete_id: Annotated[int, Field(ge=1)],
    ) -> dict[str, Any]:
        return await _call_replog(ctx, "GET", f"/api-mcp/athletes/{athlete_id}/journal")

    @mcp.tool(
        name="replog_list_athlete_programs",
        description=(
            "List all programs assigned to one athlete (active + past). "
            "Each entry includes the program template, role "
            "(primary/secondary/accessory), start date, and active flag."
        ),
    )
    async def list_athlete_programs(
        ctx: Context[Any, Any, Any],
        athlete_id: Annotated[int, Field(ge=1)],
    ) -> dict[str, Any]:
        return await _call_replog(ctx, "GET", f"/api-mcp/athletes/{athlete_id}/programs")

    @mcp.tool(
        name="replog_list_athlete_equipment",
        description=(
            "List the equipment available to one athlete. Use before "
            "any coaching conversation that involves substitutions "
            "('can we do hip thrusts? does she have a bench?')."
        ),
    )
    async def list_athlete_equipment(
        ctx: Context[Any, Any, Any],
        athlete_id: Annotated[int, Field(ge=1)],
    ) -> dict[str, Any]:
        return await _call_replog(ctx, "GET", f"/api-mcp/athletes/{athlete_id}/equipment")

    # ── Group B: clerical writes ────────────────────────────────────

    @mcp.tool(
        name="replog_create_workout",
        description=(
            "Create today's workout row for an athlete (one workout per "
            "athlete per day). Call this before replog_log_set. The "
            "response includes the new workout's ID, which you must pass "
            "to the set-logging tools. If a workout already exists for "
            "this date, RepLog returns the existing one — safe to call "
            "idempotently."
        ),
    )
    async def create_workout(
        ctx: Context[Any, Any, Any],
        athlete_id: Annotated[int, Field(ge=1)],
        date: Annotated[
            str,
            Field(
                description="ISO date YYYY-MM-DD (typically today's date in the athlete's TZ)",
                pattern=r"^\d{4}-\d{2}-\d{2}$",
            ),
        ],
    ) -> dict[str, Any]:
        return await _call_replog(
            ctx,
            "POST",
            f"/api-mcp/athletes/{athlete_id}/workouts",
            json={"date": date},
        )

    @mcp.tool(
        name="replog_log_set",
        description=(
            "Log one completed set against a workout. Use one call per "
            "set as the lifter finishes it — this is the live-logging "
            "loop. RepLog enforces set_number monotonicity per exercise "
            "and links to training maxes for downstream analysis."
        ),
    )
    async def log_set(
        ctx: Context[Any, Any, Any],
        athlete_id: Annotated[int, Field(ge=1)],
        workout_id: Annotated[int, Field(ge=1, description="From replog_create_workout")],
        exercise_id: Annotated[int, Field(ge=1)],
        set_number: Annotated[int, Field(ge=1, description="1-indexed; per-exercise sequence")],
        reps: Annotated[int, Field(ge=0, le=1000)],
        weight: Annotated[
            float, Field(ge=0, description="Weight in the athlete's unit (lbs by default)")
        ],
        rpe: Annotated[
            float | None,
            Field(ge=1, le=10, description="Rate of Perceived Exertion 1-10 (optional)"),
        ] = None,
        notes: Annotated[
            str | None,
            Field(max_length=500, description="Per-set notes (optional)"),
        ] = None,
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "exercise_id": exercise_id,
            "set_number": set_number,
            "reps": reps,
            "weight": weight,
        }
        if rpe is not None:
            payload["rpe"] = rpe
        if notes is not None:
            payload["notes"] = notes
        return await _call_replog(
            ctx,
            "POST",
            f"/api-mcp/athletes/{athlete_id}/workouts/{workout_id}/sets",
            json=payload,
        )

    @mcp.tool(
        name="replog_update_set",
        description=(
            "Edit a just-logged set (typo correction, RPE addition, "
            "notes). Pass the new value for every field — the handler "
            "treats the request as a full replacement of the set's "
            "mutable fields."
        ),
    )
    async def update_set(
        ctx: Context[Any, Any, Any],
        athlete_id: Annotated[int, Field(ge=1)],
        workout_id: Annotated[int, Field(ge=1)],
        set_id: Annotated[int, Field(ge=1, description="Set ID from the workout detail")],
        exercise_id: Annotated[int, Field(ge=1)],
        set_number: Annotated[int, Field(ge=1)],
        reps: Annotated[int, Field(ge=0, le=1000)],
        weight: Annotated[float, Field(ge=0)],
        rpe: Annotated[float | None, Field(ge=1, le=10)] = None,
        notes: Annotated[str | None, Field(max_length=500)] = None,
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "exercise_id": exercise_id,
            "set_number": set_number,
            "reps": reps,
            "weight": weight,
        }
        if rpe is not None:
            payload["rpe"] = rpe
        if notes is not None:
            payload["notes"] = notes
        return await _call_replog(
            ctx,
            "PUT",
            f"/api-mcp/athletes/{athlete_id}/workouts/{workout_id}/sets/{set_id}",
            json=payload,
        )

    @mcp.tool(
        name="replog_delete_set",
        description=(
            "Delete a logged set (mis-logged, never actually performed). "
            "Use sparingly — preferred path for an edit is "
            "replog_update_set. Cannot delete sets that have already "
            "been reviewed by a coach."
        ),
    )
    async def delete_set(
        ctx: Context[Any, Any, Any],
        athlete_id: Annotated[int, Field(ge=1)],
        workout_id: Annotated[int, Field(ge=1)],
        set_id: Annotated[int, Field(ge=1)],
    ) -> dict[str, Any]:
        return await _call_replog(
            ctx,
            "DELETE",
            f"/api-mcp/athletes/{athlete_id}/workouts/{workout_id}/sets/{set_id}",
        )

    @mcp.tool(
        name="replog_update_workout_notes",
        description=(
            "Set or replace the workout-level notes (separate from "
            "per-set notes). Use for end-of-session reflections, "
            "weather, sleep, anything the coach should see at a glance."
        ),
    )
    async def update_workout_notes(
        ctx: Context[Any, Any, Any],
        athlete_id: Annotated[int, Field(ge=1)],
        workout_id: Annotated[int, Field(ge=1)],
        notes: Annotated[str, Field(max_length=2000)],
    ) -> dict[str, Any]:
        return await _call_replog(
            ctx,
            "PUT",
            f"/api-mcp/athletes/{athlete_id}/workouts/{workout_id}/notes",
            json={"notes": notes},
        )

    @mcp.tool(
        name="replog_log_body_weight",
        description=(
            "Log a body-weight reading for an athlete. Used in "
            "percentage-program TM adjustments and for trend lines on "
            "the dashboard. RepLog stores readings indefinitely; "
            "duplicates on the same date are allowed."
        ),
    )
    async def log_body_weight(
        ctx: Context[Any, Any, Any],
        athlete_id: Annotated[int, Field(ge=1)],
        weight: Annotated[float, Field(ge=10, le=1000, description="Body weight in lbs")],
        date: Annotated[
            str | None,
            Field(
                description="ISO date YYYY-MM-DD (defaults to today on the server if omitted)",
                pattern=r"^\d{4}-\d{2}-\d{2}$",
            ),
        ] = None,
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {"weight": weight}
        if date is not None:
            payload["date"] = date
        return await _call_replog(
            ctx,
            "POST",
            f"/api-mcp/athletes/{athlete_id}/body-weights",
            json=payload,
        )

    @mcp.tool(
        name="replog_add_athlete_note",
        description=(
            "Add a coach- or athlete-authored note to the athlete's "
            "journal. Notes are visible to the coach in the webui and "
            "to LLM context-assembly when drafting programs. NOT a "
            "coaching decision — a coach saying 'left knee bothered her "
            "during squats today' is fact-recording, which is what this "
            "is for."
        ),
    )
    async def add_athlete_note(
        ctx: Context[Any, Any, Any],
        athlete_id: Annotated[int, Field(ge=1)],
        content: Annotated[str, Field(min_length=1, max_length=4000)],
        is_private: Annotated[
            bool,
            Field(description="If true, only the author sees it (rare)"),
        ] = False,
        pinned: Annotated[
            bool,
            Field(description="Surfaces at the top of the journal"),
        ] = False,
    ) -> dict[str, Any]:
        return await _call_replog(
            ctx,
            "POST",
            f"/api-mcp/athletes/{athlete_id}/notes",
            json={"content": content, "is_private": is_private, "pinned": pinned},
        )

    # ── Group C: program draft (enqueue + status only) ──────────────
    #
    # No replog_execute_generation tool — that's the doctrine. The
    # generation's executed_at timestamp is set ONLY by the webui's
    # /api/athletes/{id}/generations/{genID}/execute handler, which
    # requires a real session cookie and an explicit human click. See
    # ADR 007 / 015 / HOF-004 [forbidden].

    @mcp.tool(
        name="replog_enqueue_program_generation",
        description=(
            "Ask RepLog's AI Coach to draft a program for one athlete. "
            "Returns immediately with a generation_id; the LLM call "
            "runs in the background (typically 30-120 seconds for a "
            "multi-week program). DO NOT poll silently — return the "
            "generation_id to the user along with the instruction to "
            "either (a) call replog_get_generation_status in ~30s, "
            "or (b) open https://replog.holthome.net/generate/<athlete_id> "
            "to watch progress and approve when ready. The draft is "
            "NOT auto-applied; a coach must review + approve via the "
            "webui (this is by design — see ADR 007)."
        ),
    )
    async def enqueue_program_generation(
        ctx: Context[Any, Any, Any],
        athlete_id: Annotated[int, Field(ge=1)],
        program_name: Annotated[
            str,
            Field(
                min_length=1,
                max_length=200,
                description="Working name for the proposed program (e.g. 'Sport Performance Block 4')",
            ),
        ],
        notes: Annotated[
            str | None,
            Field(
                max_length=2000,
                description=(
                    "Optional coach intent / constraints to bias the LLM "
                    "('focus on hinge variants, no jumping for 2 weeks')"
                ),
            ),
        ] = None,
        methodology_id: Annotated[
            int | None,
            Field(
                ge=1,
                description=(
                    "Optional methodology to use (ADR 016). Omit to let "
                    "RepLog pick based on the athlete's tier."
                ),
            ),
        ] = None,
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {"program_name": program_name}
        if notes is not None:
            payload["notes"] = notes
        if methodology_id is not None:
            payload["methodology_id"] = methodology_id
        result = await _call_replog(
            ctx,
            "POST",
            f"/api-mcp/athletes/{athlete_id}/generate",
            json=payload,
        )
        # Inject an explicit poll-or-handoff instruction so the agent
        # doesn't busy-wait. This is part of the HOF-004 Group C
        # contract — keep the human in the loop.
        if "generation_id" in result:
            result.setdefault(
                "next_step",
                (
                    "Tell the user the draft is being generated and offer "
                    "to check status with replog_get_generation_status in "
                    "~30 seconds, OR direct them to open "
                    f"https://replog.holthome.net/generate/{athlete_id} "
                    "to watch progress and approve the result. The draft "
                    "is NEVER applied automatically — a human must approve "
                    "via the webui."
                ),
            )
        return result

    @mcp.tool(
        name="replog_get_generation_status",
        description=(
            "Check the status of an in-flight or completed LLM program "
            "draft. Status values: pending, running, succeeded, failed, "
            "cancelled. On 'succeeded' the response includes a brief "
            "summary of the proposed program; the FULL CatalogJSON and "
            "approval action stay on the webui — direct the user to "
            "https://replog.holthome.net/generate/<athlete_id> to review "
            "and approve."
        ),
    )
    async def get_generation_status(
        ctx: Context[Any, Any, Any],
        athlete_id: Annotated[int, Field(ge=1)],
        generation_id: Annotated[
            int, Field(ge=1, description="From replog_enqueue_program_generation")
        ],
    ) -> dict[str, Any]:
        return await _call_replog(
            ctx,
            "GET",
            f"/api-mcp/athletes/{athlete_id}/generations/{generation_id}",
        )
