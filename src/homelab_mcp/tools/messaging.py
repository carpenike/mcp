"""Signal messaging via signal-cli-rest-api.

One tool, a closed set of named destinations. Callers pass an ALIAS — "family"
or "ops" — and the server resolves it to a group id from configuration. Ids are
never accepted as input and never returned in output, so this tool structurally
cannot message an arbitrary person or number. That property is what makes it
safe to grant to an unattended agent (hermes-agent composes the weekly pulse
and sends it here).

Adding a free-form `recipient` parameter would silently convert a scoped
notifier into a general-purpose outbound messaging capability. Don't. The alias
set is a Literal precisely so the schema itself refuses a raw id.

Two destinations, deliberately separate:

  family  the shared household channel both partners read. The default, so
          existing callers are unchanged.
  ops     Ryan's operational channel, where scheduled advisor sessions deliver
          the daily morning-check report. Routine ops output does not belong in
          a channel that is read as "something needs our attention".

The ops id comes from sops and is referenced only by its alias. Keeping it out
of repo docs, prompts and tool responses is the whole point of the fingerprint
hardening in nix-config — so an unconfigured 'ops' fails loudly rather than
falling back. Guessing a Signal destination is never acceptable: the failure
mode is a private message arriving in the wrong room.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Annotated, Any, Literal

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
signal_send delivers a message to one of two named Signal destinations
configured on the server. You choose by ALIAS; there is no way to send to an
arbitrary number or group, and the ids are never exposed.

- `target` omitted or "family" — the shared household channel BOTH PARTNERS
  read. Treat it as publishing. Keep it short (the weekly pulse convention is
  <=5 lines), and reserve off-cycle sends for genuinely unusual events: a
  missed fixed payment, suspected fraud, a dead sync. Alert fatigue is the
  known failure mode for this channel.
- `target: "ops"` — Ryan's operational channel. Scheduled sessions deliver the
  daily morning-check report here. Routine automation output belongs here and
  NOT in the family group.

Compose the full message text before calling. The tool sends exactly what it
is given and never edits, prefixes, or summarizes.

If "ops" is not configured the call fails and nothing is sent. That is
deliberate — a misdirected Signal message cannot be recalled, so the tool
never falls back to another destination.
"""

# Alias -> the Settings attribute holding that destination's group id. A closed
# map, not caller input: the id is looked up here and never travels either
# direction across the tool boundary.
_TARGET_SETTING = {
    "family": "signal_group_id",
    "ops": "signal_ops_group_id",
}
DEFAULT_TARGET = "family"


def register(mcp: FastMCP, settings: Settings) -> None:
    """Register the signal_send tool on the given MCP server."""
    base = settings.signal_base_url.rstrip("/")
    client = make_client(timeout=30.0)

    @mcp.tool(
        annotations=_SEND,
        name="signal_send",
        description=(
            "Send a message to one of two named Signal destinations. Choose "
            "with `target`: omit it (or pass 'family') for the shared household "
            "group both partners read — the weekly financial pulse and "
            "off-cycle alerts about something genuinely unusual; pass 'ops' for "
            "Ryan's operational channel, where scheduled sessions deliver the "
            "daily morning-check report. Routine automation output goes to "
            "'ops', not to the family group. Recipients are resolved "
            "server-side from these aliases: this tool accepts no phone number "
            "or group id and never reveals one, so it cannot message anyone "
            "outside the configured set. Compose the complete message text "
            "yourself first — it is sent verbatim, with no edits or additions. "
            "Messages must be non-empty and at most 2000 characters. If the "
            "requested target is not configured the call fails and nothing is "
            "sent; it never falls back to another destination."
        ),
    )
    async def signal_send(
        message: Annotated[
            str,
            Field(description="Exact message text to send. Sent verbatim."),
        ],
        target: Annotated[
            Literal["family", "ops"] | None,
            Field(
                description=(
                    "Which configured destination to send to. 'family' (the "
                    "default) is the shared household group; 'ops' is Ryan's "
                    "operational channel for scheduled reports."
                )
            ),
        ] = None,
    ) -> dict[str, Any]:
        started = datetime.now(UTC)
        # Named before the try block so the failure audit can record it.
        alias = (target or DEFAULT_TARGET).strip().lower() if target is not None else DEFAULT_TARGET
        try:
            if not base or not settings.signal_number:
                raise ToolError(
                    "signal_not_configured",
                    "Signal messaging is not configured.",
                    "Set HOMELAB_MCP_SIGNAL_BASE_URL and _NUMBER.",
                )
            # Re-checked at the tool boundary rather than trusting the schema's
            # enum, so a client that bypasses the JSON-schema layer still cannot
            # smuggle a raw group id in through `target`.
            if alias not in _TARGET_SETTING:
                raise ToolError(
                    "signal_unknown_target",
                    f"No Signal destination named {alias!r}.",
                    "Valid targets: " + ", ".join(sorted(_TARGET_SETTING)) + ". "
                    "This tool takes an alias, never a number or group id.",
                )
            # Resolved at call time, so a re-read of settings takes effect
            # without re-registering the tool.
            recipient = str(getattr(settings, _TARGET_SETTING[alias], "") or "")
            if not recipient:
                raise ToolError(
                    "signal_target_not_configured",
                    f"The {alias!r} Signal destination is not configured.",
                    # The env var name is safe to name; its value is not.
                    f"Set HOMELAB_MCP_{_TARGET_SETTING[alias].upper()}. Nothing was "
                    "sent — this tool will not fall back to another destination, "
                    "because a misdirected Signal message cannot be recalled.",
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
                # Resolved from the alias above; never caller-supplied.
                "recipients": [recipient],
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
                    # Alias only. The group id stays out of the log for the same
                    # reason it stays out of the response: logs get pasted.
                    audit.info(
                        "signal_send ok at=%s target=%s chars=%d attempt=%d timestamp=%s",
                        started.isoformat(),
                        alias,
                        len(text),
                        attempt,
                        timestamp,
                    )
                    return {
                        "sent": True,
                        "target": alias,
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
                "signal_send FAILED at=%s target=%s chars=%d code=%s",
                started.isoformat(),
                alias,
                len((message or "").strip()),
                err.code,
            )
            return err.payload()
