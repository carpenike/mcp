"""Home Assistant tools — states, closed-loop service calls, automations.

Home Assistant is a PHYSICAL control plane, so this module is stricter
than the data-shaped categories (grocy, cooklang):

  - `ha_call_service` refuses any domain not on the operator-configured
    allowlist (`Settings.ha_domain_allowlist`), checked for BOTH the
    service domain and the target entity's domain. The callable surface
    is operator-chosen, never model-chosen.
  - High-impact domains (`Settings.ha_confirm_domains`, e.g. lock/alarm/
    cover) additionally require `confirm=true`; without it the tool
    returns a non-destructive preview (the cooklang_delete_recipe pattern).
  - Every actuation closes its own loop: HA acknowledges a service call
    when it is *dispatched*, not when the device changed, so the tool
    reads the entity before the call, polls it after (up to
    `Settings.ha_confirm_timeout_seconds`), and reports observed
    before/after state plus an honest `confirmed` flag. A tool must never
    report intent as outcome.
  - Automation edits go through HA's config API
    (`/api/config/automation/config/<id>`) — the same endpoints the HA UI
    editor uses — so HA structurally validates and hot-reloads every
    write. This service never touches HA's config directory (see
    AGENTS.md security non-negotiable #8).
  - Writes emit an audit line (tool, target, arguments, outcome) on the
    `homelab_mcp.audit` logger so "why did X actuate at 3am" is
    answerable from journald. The HA token is never logged.

Authentication: every request carries `Authorization: Bearer <token>`
loaded from `Settings.ha_token` (sops-managed env var). It NEVER comes
from user input. The automation config-API tools require the token's HA
user to be an administrator; the state/service tools do not.

Tool name convention: `ha_<verb>_<object>`. See AGENTS.md.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import time
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Annotated, Any

from pydantic import Field

from homelab_mcp.tools._http import ToolError, enc, make_client, request_json

if TYPE_CHECKING:
    from mcp.server.fastmcp import FastMCP

    from homelab_mcp.config import Settings

log = logging.getLogger(__name__)

# Dedicated audit trail for actuations. RequestLogMiddleware only sees
# `POST /mcp`, so tool-level writes must self-report to be reconstructable.
audit = logging.getLogger("homelab_mcp.audit")

# HA entity ids are `<domain>.<object_id>`, lowercase snake in both halves.
ENTITY_RE = re.compile(r"^[a-z0-9_]+\.[a-z0-9_]+$")
# Service and domain names share the same single-segment shape.
NAME_RE = re.compile(r"^[a-z0-9_]+$")
# Automation ids (HA UI uses epoch-millis strings; slugs are also legal).
AUTOMATION_ID_RE = re.compile(r"^[A-Za-z0-9_-]+$")

# Services whose outcome maps to a known target state, letting the
# post-call poll wait for the *right* state instead of just "any update".
_EXPECTED_STATE = {
    "turn_on": "on",
    "turn_off": "off",
    "lock": "locked",
    "unlock": "unlocked",
    "open_cover": "open",
    "close_cover": "closed",
}

# Poll cadence for the closed-loop confirmation read-back.
_POLL_INTERVAL = 0.25

# Bound on how long we wait for any single HA call (the config check runs
# on a separate, slower client — a full core check can take a while).
_TIMEOUT = 15.0
_SLOW_TIMEOUT = 60.0


def _compact(state: dict[str, Any]) -> dict[str, Any]:
    """Project an HA state object to the fields the conversation needs."""
    attrs = state.get("attributes") or {}
    return {
        "entity_id": state.get("entity_id"),
        "state": state.get("state"),
        "friendly_name": attrs.get("friendly_name"),
        "last_changed": state.get("last_changed"),
        "last_updated": state.get("last_updated"),
    }


def register(mcp: FastMCP, settings: Settings) -> None:
    """Register ha_* Home Assistant tools on the given MCP server."""
    base = settings.ha_base_url.rstrip("/")
    token = settings.ha_token
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    client = make_client(headers=headers, timeout=_TIMEOUT)
    slow_client = make_client(headers=headers, timeout=_SLOW_TIMEOUT)

    allowed_domains = {d.strip().lower() for d in settings.ha_domain_allowlist if d.strip()}
    confirm_domains = {d.strip().lower() for d in settings.ha_confirm_domains if d.strip()}
    confirm_timeout = settings.ha_confirm_timeout_seconds

    def _ensure_configured() -> None:
        if not base or not token:
            raise ToolError(
                "ha_not_configured",
                "Home Assistant is not configured on this server.",
                "Set HOMELAB_MCP_HA_BASE_URL and HOMELAB_MCP_HA_TOKEN "
                "(token via the sops-managed environment file).",
            )

    def _validate_entity(entity_id: str) -> None:
        if not ENTITY_RE.match(entity_id):
            raise ToolError(
                "ha_invalid_entity_id",
                f"'{entity_id}' is not a valid entity id.",
                "Expected '<domain>.<object_id>' in lowercase snake_case, "
                "e.g. 'light.office_desk_lamp'.",
            )

    async def _get_state(entity_id: str) -> dict[str, Any] | None:
        """Fetch one entity's state; None when HA doesn't know the entity."""
        try:
            data = await request_json(
                client,
                "GET",
                f"{base}/api/states/{enc(entity_id)}",
                service="ha",
                unreachable_hint="Check HOMELAB_MCP_HA_BASE_URL and that Home Assistant is up.",
            )
        except ToolError as err:
            if err.code == "ha_http_404":
                return None
            raise
        return data if isinstance(data, dict) else None

    async def _get_automation_config(automation_id: str) -> dict[str, Any] | None:
        """Fetch one automation's config via the config API; None when absent."""
        try:
            data = await request_json(
                client,
                "GET",
                f"{base}/api/config/automation/config/{enc(automation_id)}",
                service="ha",
                unreachable_hint="Check HOMELAB_MCP_HA_BASE_URL and that Home Assistant is up.",
            )
        except ToolError as err:
            if err.code == "ha_http_404":
                return None
            if err.code == "ha_http_401" or err.code == "ha_http_403":
                raise ToolError(
                    err.code,
                    "Home Assistant rejected the automation config-API call.",
                    "The config API requires the HOMELAB_MCP_HA_TOKEN user to be "
                    "an HA administrator.",
                ) from err
            raise
        return data if isinstance(data, dict) else None

    # ── health ───────────────────────────────────────────────────────
    @mcp.tool(
        name="ha_health",
        description=(
            "Check connectivity to Home Assistant and report its version and "
            "location name. Use first when other ha_* tools fail, to tell "
            "'HA is down / unreachable / token rejected' apart from a "
            "tool-specific problem."
        ),
    )
    async def health() -> dict[str, Any]:
        try:
            _ensure_configured()
            info = await request_json(
                client,
                "GET",
                f"{base}/api/config",
                service="ha",
                unreachable_hint="Check HOMELAB_MCP_HA_BASE_URL and that Home Assistant is up.",
            )
        except ToolError as err:
            return err.payload()
        info = info if isinstance(info, dict) else {}
        return {
            "ok": True,
            "version": info.get("version"),
            "location_name": info.get("location_name"),
            "state": info.get("state"),
        }

    # ── list entities ────────────────────────────────────────────────
    @mcp.tool(
        name="ha_list_entities",
        description=(
            "List Home Assistant entities with their current state, filtered "
            "by domain (e.g. 'light', 'switch', 'sensor') and/or a free-text "
            "search over entity id and friendly name. Use this to find the "
            "exact entity_id before calling ha_get_state or ha_call_service — "
            "never guess an entity_id. Results are capped; the response "
            "reports returned/total/truncated."
        ),
    )
    async def list_entities(
        domain: Annotated[
            str | None,
            Field(description="Filter to one domain, e.g. 'light' (optional)"),
        ] = None,
        search: Annotated[
            str | None,
            Field(description="Case-insensitive substring over entity_id + friendly name"),
        ] = None,
        limit: Annotated[int, Field(ge=1, le=200)] = 50,
    ) -> dict[str, Any]:
        try:
            _ensure_configured()
            if domain is not None and not NAME_RE.match(domain):
                raise ToolError(
                    "ha_invalid_domain",
                    f"'{domain}' is not a valid domain name.",
                    "Use lowercase snake_case, e.g. 'light' or 'binary_sensor'.",
                )
            states = await request_json(
                client,
                "GET",
                f"{base}/api/states",
                service="ha",
                unreachable_hint="Check HOMELAB_MCP_HA_BASE_URL and that Home Assistant is up.",
            )
        except ToolError as err:
            return err.payload()

        rows = [_compact(s) for s in (states or []) if isinstance(s, dict)]
        if domain is not None:
            prefix = domain + "."
            rows = [r for r in rows if str(r["entity_id"] or "").startswith(prefix)]
        if search:
            needle = search.lower()
            rows = [
                r
                for r in rows
                if needle in str(r["entity_id"] or "").lower()
                or needle in str(r["friendly_name"] or "").lower()
            ]
        rows.sort(key=lambda r: str(r["entity_id"] or ""))
        total = len(rows)
        return {
            "returned": min(total, limit),
            "total": total,
            "truncated": total > limit,
            "entities": rows[:limit],
        }

    # ── get one entity ───────────────────────────────────────────────
    @mcp.tool(
        name="ha_get_state",
        description=(
            "Get one Home Assistant entity's full current state, including "
            "all attributes and last_changed/last_updated timestamps. Use to "
            "verify actual device state — e.g. when the user says a device "
            "didn't respond, re-check here instead of assuming the last "
            "service call worked."
        ),
    )
    async def get_state(
        entity_id: Annotated[
            str,
            Field(description="Full entity id, e.g. 'light.office_desk_lamp'"),
        ],
    ) -> dict[str, Any]:
        try:
            _ensure_configured()
            _validate_entity(entity_id)
            state = await _get_state(entity_id)
        except ToolError as err:
            return err.payload()
        if state is None:
            return ToolError(
                "ha_entity_not_found",
                f"Home Assistant has no entity '{entity_id}'.",
                "Find the exact id with ha_list_entities.",
            ).payload()
        attrs = state.get("attributes") or {}
        return {
            **_compact(state),
            "attributes": attrs,
            "available": state.get("state") not in ("unavailable", "unknown"),
            "assumed_state": bool(attrs.get("assumed_state")),
        }

    # ── entity history ───────────────────────────────────────────────
    @mcp.tool(
        name="ha_get_history",
        description=(
            "Get one entity's recent state history (state + timestamp per "
            "change) over the last N hours. Use for 'when did the light turn "
            "on', 'how long was the door open', or to check whether a "
            "device actually changed around a given time."
        ),
    )
    async def get_history(
        entity_id: Annotated[
            str,
            Field(description="Full entity id, e.g. 'binary_sensor.front_door'"),
        ],
        hours: Annotated[int, Field(ge=1, le=168)] = 24,
        limit: Annotated[int, Field(ge=1, le=500)] = 100,
    ) -> dict[str, Any]:
        try:
            _ensure_configured()
            _validate_entity(entity_id)
            start = (datetime.now(UTC) - timedelta(hours=hours)).isoformat()
            data = await request_json(
                client,
                "GET",
                f"{base}/api/history/period/{start}",
                service="ha",
                params={
                    "filter_entity_id": entity_id,
                    "minimal_response": "true",
                    "no_attributes": "true",
                },
                unreachable_hint="Check HOMELAB_MCP_HA_BASE_URL and that Home Assistant is up.",
            )
        except ToolError as err:
            return err.payload()
        raw = data[0] if isinstance(data, list) and data else []
        rows = [
            {"state": r.get("state"), "last_changed": r.get("last_changed")}
            for r in raw
            if isinstance(r, dict)
        ]
        total = len(rows)
        # Keep the most recent rows when over the cap.
        kept = rows[-limit:]
        return {
            "entity_id": entity_id,
            "hours": hours,
            "returned": len(kept),
            "total": total,
            "truncated": total > limit,
            "history": kept,
        }

    # ── call a service (closed loop) ─────────────────────────────────
    @mcp.tool(
        name="ha_call_service",
        description=(
            "Call a Home Assistant service on ONE entity (e.g. domain='light', "
            "service='turn_on', entity_id='light.office_desk_lamp', with "
            "optional data like {'brightness_pct': 40}). Only operator-"
            "allowlisted domains are callable; high-impact domains (locks, "
            "alarms, covers) also require confirm=true and return a preview "
            "without it. The tool re-reads the entity after the call and "
            "returns observed before/after state with an honest 'confirmed' "
            "flag — confirmed=false means HA accepted the call but the entity "
            "did not report the expected change in time (device may be slow, "
            "unreachable, or optimistic/assumed-state). Never tell the user "
            "an action succeeded unless confirmed is true; on "
            "confirmed=false, say so and check ha_get_state."
        ),
    )
    async def call_service(
        domain: Annotated[str, Field(description="Service domain, e.g. 'light'")],
        service: Annotated[str, Field(description="Service name, e.g. 'turn_on'")],
        entity_id: Annotated[
            str,
            Field(description="Target entity, e.g. 'light.office_desk_lamp'"),
        ],
        data: Annotated[
            dict[str, Any] | None,
            Field(description="Extra service data, e.g. {'brightness_pct': 40}"),
        ] = None,
        confirm: Annotated[
            bool,
            Field(description="Required true for high-impact domains (lock/alarm/cover/...)"),
        ] = False,
    ) -> dict[str, Any]:
        try:
            _ensure_configured()
            if not NAME_RE.match(domain):
                raise ToolError("ha_invalid_domain", f"'{domain}' is not a valid domain name.", "")
            if not NAME_RE.match(service):
                raise ToolError(
                    "ha_invalid_service", f"'{service}' is not a valid service name.", ""
                )
            _validate_entity(entity_id)
            entity_domain = entity_id.split(".", 1)[0]
            for d in (domain, entity_domain):
                if d not in allowed_domains:
                    audit.info(
                        "ha_call_service DENIED domain=%s service=%s entity=%s "
                        "(domain %r not allowlisted)",
                        domain,
                        service,
                        entity_id,
                        d,
                    )
                    raise ToolError(
                        "ha_domain_not_allowed",
                        f"Domain '{d}' is not on this server's allowlist.",
                        "The callable domains are operator-configured via "
                        "HOMELAB_MCP_HA_DOMAIN_ALLOWLIST; this is deliberate and "
                        "not overridable from the conversation.",
                    )

            before = await _get_state(entity_id)
            if before is None:
                raise ToolError(
                    "ha_entity_not_found",
                    f"Home Assistant has no entity '{entity_id}'.",
                    "Find the exact id with ha_list_entities.",
                )

            needs_confirm = domain in confirm_domains or entity_domain in confirm_domains
            if needs_confirm and not confirm:
                audit.info(
                    "ha_call_service PREVIEW domain=%s service=%s entity=%s data=%s "
                    "(confirm required, not given)",
                    domain,
                    service,
                    entity_id,
                    json.dumps(data or {}, sort_keys=True),
                )
                return {
                    "executed": False,
                    "preview": {
                        "domain": domain,
                        "service": service,
                        "entity": _compact(before),
                        "data": data or {},
                    },
                    "hint": (
                        "This domain is high-impact and requires confirmation. "
                        "Confirm the exact target with the user, then re-call "
                        "with confirm=true."
                    ),
                }

            audit.info(
                "ha_call_service EXECUTE domain=%s service=%s entity=%s data=%s",
                domain,
                service,
                entity_id,
                json.dumps(data or {}, sort_keys=True),
            )
            await request_json(
                client,
                "POST",
                f"{base}/api/services/{enc(domain)}/{enc(service)}",
                service="ha",
                json={**(data or {}), "entity_id": entity_id},
                unreachable_hint="Check HOMELAB_MCP_HA_BASE_URL and that Home Assistant is up.",
            )

            # Closed loop: HA has only DISPATCHED the call at this point.
            # Poll the entity until it converges (or the deadline passes) so
            # `confirmed` reflects observed state, never intent.
            before_state = str(before.get("state"))
            expected = _EXPECTED_STATE.get(service)
            if service == "toggle":
                expected = {"on": "off", "off": "on"}.get(before_state)

            after: dict[str, Any] = before
            confirmed = False
            deadline = time.monotonic() + confirm_timeout
            while True:
                polled = await _get_state(entity_id)
                if polled is not None:
                    after = polled
                    if expected is not None:
                        confirmed = str(after.get("state")) == expected
                    else:
                        confirmed = (
                            after.get("last_updated") != before.get("last_updated")
                            or str(after.get("state")) != before_state
                        )
                    if confirmed:
                        break
                if time.monotonic() >= deadline:
                    break
                await asyncio.sleep(min(_POLL_INTERVAL, confirm_timeout))
        except ToolError as err:
            return err.payload()

        after_attrs = after.get("attributes") or {}
        assumed = bool(after_attrs.get("assumed_state"))
        result: dict[str, Any] = {
            "executed": True,
            "requested": {
                "domain": domain,
                "service": service,
                "entity_id": entity_id,
                "data": data or {},
            },
            "before": _compact(before),
            "after": _compact(after),
            "confirmed": confirmed,
            "assumed_state": assumed,
        }
        if not confirmed:
            result["note"] = (
                f"HA accepted the service call, but '{entity_id}' did not report "
                f"the expected change within {confirm_timeout:g}s. The device may "
                "be slow, unreachable, or asleep. Tell the user the outcome is "
                "unverified and re-check with ha_get_state."
            )
        elif assumed:
            result["note"] = (
                "This device uses optimistic (assumed) state: HA records the "
                "commanded state without device confirmation, so 'confirmed' "
                "reflects HA's database, not a report from the device."
            )
        audit.info(
            "ha_call_service RESULT domain=%s service=%s entity=%s confirmed=%s before=%s after=%s",
            domain,
            service,
            entity_id,
            confirmed,
            before.get("state"),
            after.get("state"),
        )
        return result

    # ── automations: list ────────────────────────────────────────────
    @mcp.tool(
        name="ha_list_automations",
        description=(
            "List Home Assistant automations with enabled state, last-"
            "triggered time, and their config-API id. Automations without an "
            "id are managed as hand-written YAML (packages/includes) and are "
            "NOT editable via ha_get_automation / ha_upsert_automation — "
            "those belong to the git-managed config."
        ),
    )
    async def list_automations() -> dict[str, Any]:
        try:
            _ensure_configured()
            states = await request_json(
                client,
                "GET",
                f"{base}/api/states",
                service="ha",
                unreachable_hint="Check HOMELAB_MCP_HA_BASE_URL and that Home Assistant is up.",
            )
        except ToolError as err:
            return err.payload()
        rows = []
        for s in states or []:
            if not isinstance(s, dict):
                continue
            eid = str(s.get("entity_id") or "")
            if not eid.startswith("automation."):
                continue
            attrs = s.get("attributes") or {}
            rows.append(
                {
                    "entity_id": eid,
                    "friendly_name": attrs.get("friendly_name"),
                    "enabled": s.get("state") == "on",
                    "last_triggered": attrs.get("last_triggered"),
                    "id": attrs.get("id"),
                    "editable_via_api": attrs.get("id") is not None,
                }
            )
        rows.sort(key=lambda r: str(r["entity_id"]))
        return {"returned": len(rows), "total": len(rows), "truncated": False, "automations": rows}

    # ── automations: read one ────────────────────────────────────────
    @mcp.tool(
        name="ha_get_automation",
        description=(
            "Fetch one automation's full configuration (triggers, conditions, "
            "actions) by its config-API id — the 'id' field from "
            "ha_list_automations, NOT the entity_id. Requires the server's HA "
            "token to belong to an administrator."
        ),
    )
    async def get_automation(
        automation_id: Annotated[
            str,
            Field(description="Config-API id from ha_list_automations (not the entity_id)"),
        ],
    ) -> dict[str, Any]:
        try:
            _ensure_configured()
            if not AUTOMATION_ID_RE.match(automation_id):
                raise ToolError(
                    "ha_invalid_automation_id",
                    f"'{automation_id}' is not a valid automation id.",
                    "Use the 'id' field from ha_list_automations.",
                )
            config = await _get_automation_config(automation_id)
        except ToolError as err:
            return err.payload()
        if config is None:
            return ToolError(
                "ha_automation_not_found",
                f"No automation with id '{automation_id}'.",
                "Only automations with an id (see ha_list_automations) are "
                "reachable via the config API; hand-written YAML automations "
                "are managed in the config repo instead.",
            ).payload()
        return {"automation_id": automation_id, "config": config}

    # ── automations: create/update ───────────────────────────────────
    @mcp.tool(
        name="ha_upsert_automation",
        description=(
            "Create or update ONE automation through Home Assistant's config "
            "API — the same validated path the HA UI editor uses; HA "
            "structurally validates the config, writes it, and hot-reloads "
            "it. Pass the full desired config (alias/triggers/conditions/"
            "actions) as a JSON object. Without confirm=true this returns a "
            "non-destructive preview showing the existing config next to the "
            "proposed one — always show the user that diff before confirming. "
            "Requires an administrator HA token."
        ),
    )
    async def upsert_automation(
        automation_id: Annotated[
            str,
            Field(
                description=(
                    "Config-API id. Use an existing id (from ha_list_automations) "
                    "to update, or a fresh slug/epoch-millis string to create."
                )
            ),
        ],
        config: Annotated[
            dict[str, Any],
            Field(description="Full automation config: alias, triggers, conditions, actions"),
        ],
        confirm: Annotated[
            bool,
            Field(description="Required true to write; false returns a preview"),
        ] = False,
    ) -> dict[str, Any]:
        try:
            _ensure_configured()
            if not AUTOMATION_ID_RE.match(automation_id):
                raise ToolError(
                    "ha_invalid_automation_id",
                    f"'{automation_id}' is not a valid automation id.",
                    "Use letters, digits, '_' or '-'.",
                )
            if not config:
                raise ToolError(
                    "ha_invalid_automation_config",
                    "The automation config must be a non-empty object.",
                    "Provide at least alias + triggers + actions.",
                )
            existing = await _get_automation_config(automation_id)
            action = "update" if existing is not None else "create"

            if not confirm:
                audit.info(
                    "ha_upsert_automation PREVIEW id=%s action=%s (confirm not given)",
                    automation_id,
                    action,
                )
                return {
                    "executed": False,
                    "preview": {
                        "action": action,
                        "automation_id": automation_id,
                        "existing": existing,
                        "proposed": config,
                    },
                    "hint": (
                        "Show the user this change, then re-call with "
                        "confirm=true to write. HA validates and hot-reloads "
                        "the automation on write."
                    ),
                }

            audit.info(
                "ha_upsert_automation EXECUTE id=%s action=%s config=%s",
                automation_id,
                action,
                json.dumps(config, sort_keys=True, default=str),
            )
            await request_json(
                slow_client,
                "POST",
                f"{base}/api/config/automation/config/{enc(automation_id)}",
                service="ha",
                # HA stores the id inside the stored object; keep them in lock-step.
                json={**config, "id": automation_id},
                unreachable_hint="Check HOMELAB_MCP_HA_BASE_URL and that Home Assistant is up.",
            )
            # Close the loop: return what HA actually stored, not what we sent.
            stored = await _get_automation_config(automation_id)
        except ToolError as err:
            return err.payload()
        audit.info("ha_upsert_automation RESULT id=%s action=%s ok=True", automation_id, action)
        return {
            "executed": True,
            "action": action,
            "automation_id": automation_id,
            "config": stored,
        }

    # ── config check ─────────────────────────────────────────────────
    @mcp.tool(
        name="ha_check_config",
        description=(
            "Ask Home Assistant to validate its full configuration "
            "(equivalent of Developer Tools → Check configuration). Use "
            "before/after automation changes or when HA behaves oddly after "
            "an edit. Returns HA's verdict and any errors. Can take tens of "
            "seconds on a large config."
        ),
    )
    async def check_config() -> dict[str, Any]:
        try:
            _ensure_configured()
            data = await request_json(
                slow_client,
                "POST",
                f"{base}/api/config/core/check_config",
                service="ha",
                unreachable_hint="Check HOMELAB_MCP_HA_BASE_URL and that Home Assistant is up.",
            )
        except ToolError as err:
            return err.payload()
        data = data if isinstance(data, dict) else {}
        return {
            "valid": data.get("result") == "valid",
            "result": data.get("result"),
            "errors": data.get("errors"),
            "warnings": data.get("warnings"),
        }
