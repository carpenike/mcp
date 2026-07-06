"""Homelab uptime monitoring tools (gatus).

Wraps the local gatus instance to give Claude visibility into "is
anything broken?" style questions. Read-only; all gatus state lives
in gatus, we just project it.

Tool name convention: `homelab_<verb>_<object>`. See AGENTS.md.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Annotated, Any

from pydantic import Field

from homelab_mcp.tools._http import ToolError, enc, make_client, request_json

if TYPE_CHECKING:
    from mcp.server.fastmcp import FastMCP

    from homelab_mcp.config import Settings

log = logging.getLogger(__name__)


def register(mcp: FastMCP, settings: Settings) -> None:
    """Register homelab_* gatus-backed tools on the given MCP server."""
    base = settings.gatus_base_url.rstrip("/")
    # One pooled client for the lifetime of the process (see _http.make_client).
    client = make_client(timeout=15.0)

    # ── list endpoints + their current status ───────────────────────
    @mcp.tool(
        name="homelab_list_status",
        description=(
            "List every monitored homelab endpoint with its current health. "
            "Returns total/up/down counts and a per-endpoint summary "
            "(group, name, current status, latest check timestamp, 7-day "
            "uptime). Use for 'is anything broken?' or 'how is the "
            "homelab looking?' style questions. For deeper history on a "
            "specific endpoint, follow up with `homelab_get_endpoint_history`."
        ),
    )
    async def list_status() -> dict[str, Any]:
        try:
            raw = await request_json(
                client,
                "GET",
                f"{base}/api/v1/endpoints/statuses",
                service="gatus",
                unreachable_hint="Check HOMELAB_MCP_GATUS_BASE_URL and that gatus is up.",
            )
        except ToolError as err:
            return err.payload()

        summary: list[dict[str, Any]] = []
        for endpoint in raw or []:
            results = endpoint.get("results") or []
            latest = results[-1] if results else None
            uptime = endpoint.get("uptime") or {}
            summary.append(
                {
                    "group": endpoint.get("group"),
                    "name": endpoint.get("name"),
                    "key": endpoint.get("key"),
                    "current_status": (
                        "up"
                        if latest and latest.get("success")
                        else "down"
                        if latest
                        else "unknown"
                    ),
                    "latest_check": latest.get("timestamp") if latest else None,
                    "uptime_7d": uptime.get("7d"),
                    "uptime_24h": uptime.get("24h"),
                }
            )

        up = sum(1 for s in summary if s["current_status"] == "up")
        down = sum(1 for s in summary if s["current_status"] == "down")
        unknown = sum(1 for s in summary if s["current_status"] == "unknown")

        return {
            "total": len(summary),
            "up": up,
            "down": down,
            "unknown": unknown,
            "endpoints": summary,
        }

    # ── history of one endpoint ─────────────────────────────────────
    @mcp.tool(
        name="homelab_get_endpoint_history",
        description=(
            "Get the recent check history for one specific monitored "
            "endpoint. Use the `key` field returned by `homelab_list_status` "
            "(format: 'group_name'). Returns the most recent check results "
            "including response time, HTTP status, and which conditions "
            "passed or failed. Use when an endpoint is showing as down or "
            "degraded and you want to know what changed."
        ),
    )
    async def get_endpoint_history(
        key: Annotated[
            str,
            Field(description="Endpoint key in 'group_name' format (from list_status)"),
        ],
        limit: Annotated[int, Field(ge=1, le=50)] = 20,
    ) -> dict[str, Any]:
        # `key` is client-supplied — encode it as a single path segment so a
        # value like '../../other' can't rewrite the request path.
        try:
            data = await request_json(
                client,
                "GET",
                f"{base}/api/v1/endpoints/{enc(key)}/statuses",
                service="gatus",
                params={"page": 1, "pageSize": limit},
                unreachable_hint="Check HOMELAB_MCP_GATUS_BASE_URL and that gatus is up.",
            )
        except ToolError as err:
            return err.payload()
        return {"key": key, "results": data}
