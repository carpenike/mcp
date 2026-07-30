"""Signal messaging via signal-cli-rest-api.

One tool, one recipient. The destination group is fixed in configuration and
is NOT a tool parameter, so this tool structurally cannot message an arbitrary
person or number — that property is what makes it safe to grant to an
unattended agent (hermes-agent composes the weekly pulse and sends it here).

Adding a `recipient` parameter would silently convert a scoped notifier into a
general-purpose outbound messaging capability. Don't.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Annotated, Any

import httpx
from mcp.types import ToolAnnotations
from pydantic import Field

from homelab_mcp.tools._http import ToolError, make_client

if TYPE_CHECKING:
    from mcp.server.fastmcp import FastMCP

    from homelab_mcp.config import Settings

log = logging.getLogger(__name__)
# Sends are invisible to RequestLogMiddleware (it only sees `POST /mcp`), so
# every send is recorded on the audit logger — same pattern as ha_call_service.
audit = logging.getLogger("homelab_mcp.audit")

# Sending is a real-world side effect: not read-only, not idempotent (a retry
# can double-post), but not destructive either. openWorld is False — the
# upstream is signal-cli-rest-api, a fixed internal service on loopback.
_SEND = ToolAnnotations(
    readOnlyHint=False, destructiveHint=False, idempotentHint=False, openWorldHint=False
)

INSTRUCTIONS = """\
signal_send delivers a message to ONE fixed family Signal group configured on
the server; there is no way to choose a different recipient. Treat it as
publishing to a shared household channel that both partners read:

- Compose the full message text before calling. The tool sends exactly what it
  is given and never edits, prefixes, or summarizes.
- Keep it short. The weekly pulse convention is <=5 lines.
- Off-cycle sends should be reserved for genuinely unusual events (a missed
  fixed payment, suspected fraud, a dead sync). Alert fatigue is the known
  failure mode for this channel.
"""


def register(mcp: FastMCP, settings: Settings) -> None:
    """Register the signal_send tool on the given MCP server."""
    base = settings.signal_base_url.rstrip("/")
    client = make_client(timeout=30.0)

    @mcp.tool(
        annotations=_SEND,
        name="signal_send",
        description=(
            "Send a message to the family Signal group. The recipient is fixed "
            "in server configuration — this tool cannot message anyone else, and "
            "takes no recipient parameter. Use it to deliver the weekly financial "
            "pulse, or an off-cycle alert about something genuinely unusual. "
            "Compose the complete message text yourself first: it is sent "
            "verbatim, with no edits or additions. Messages must be non-empty "
            "and at most 2000 characters."
        ),
    )
    async def signal_send(
        message: Annotated[
            str,
            Field(description="Exact message text to send. Sent verbatim."),
        ],
    ) -> dict[str, Any]:
        started = datetime.now(UTC)
        try:
            if not base or not settings.signal_number or not settings.signal_group_id:
                raise ToolError(
                    "signal_not_configured",
                    "Signal messaging is not configured.",
                    "Set HOMELAB_MCP_SIGNAL_BASE_URL, _NUMBER and _GROUP_ID.",
                )
            text = (message or "").strip()
            if not text:
                raise ToolError(
                    "signal_empty_message",
                    "Refusing to send an empty message.",
                    "Provide non-whitespace message text.",
                )
            limit = settings.signal_max_message_chars
            if len(text) > limit:
                raise ToolError(
                    "signal_message_too_long",
                    f"Message is {len(text)} characters; the limit is {limit}.",
                    "Shorten the message, or split the content across a later send.",
                )

            payload = {
                "message": text,
                "number": settings.signal_number,
                # The single allowed recipient, straight from config.
                "recipients": [settings.signal_group_id],
            }

            # One retry, and only for transport errors or 5xx — a 4xx is a bad
            # request that will fail identically the second time, and retrying
            # a send that may have already landed risks double-posting.
            last: Exception | str | None = None
            for attempt in (1, 2):
                try:
                    resp = await client.post(f"{base}/v2/send", json=payload)
                except httpx.HTTPError as exc:
                    last = exc
                    log.warning(
                        "signal send attempt %d failed: %s", attempt, exc.__class__.__name__
                    )
                    if attempt == 2:
                        break
                    await asyncio.sleep(1.0)
                    continue

                if resp.status_code < 400:
                    timestamp = None
                    try:
                        body = resp.json()
                        if isinstance(body, dict):
                            timestamp = body.get("timestamp")
                    except ValueError:
                        pass
                    audit.info(
                        "signal_send ok at=%s chars=%d attempt=%d timestamp=%s",
                        started.isoformat(),
                        len(text),
                        attempt,
                        timestamp,
                    )
                    return {
                        "sent": True,
                        "chars": len(text),
                        "attempts": attempt,
                        "sent_at": started.isoformat(),
                        "timestamp": timestamp,
                    }

                if resp.status_code < 500:
                    detail = (resp.text or "")[:200]
                    raise ToolError(
                        f"signal_http_{resp.status_code}",
                        f"signal-cli-rest-api rejected the send: {detail}",
                        "Check the configured number and group ID are registered.",
                    )
                last = f"HTTP {resp.status_code}"
                if attempt == 2:
                    break
                await asyncio.sleep(1.0)

            raise ToolError(
                "signal_send_failed",
                f"Could not send after 2 attempts ({last}).",
                "Check that signal-cli-rest-api is running and the number is registered.",
            )
        except ToolError as err:
            audit.warning(
                "signal_send FAILED at=%s chars=%d code=%s",
                started.isoformat(),
                len((message or "").strip()),
                err.code,
            )
            return err.payload()
