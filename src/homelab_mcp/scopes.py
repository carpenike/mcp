"""Per-token tool allowlisting.

Most clients here are interactive advisor sessions and get every tool. One
client is not: hermes-agent runs unattended on a schedule, composes the weekly
financial pulse from four read-only summaries, and sends it to a Signal group.
It has no business reading raw transactions, touching documents, or actuating
anything in the house.

Rather than trusting a prompt to keep hermes in its lane, the remit is enforced
here, at dispatch. A token whose `scope` claim names an entry in
`settings.restricted_scopes` may call ONLY the tools on that entry's list:

  - `tools/call` for anything else is refused with a JSON-RPC error. This is
    the security boundary and it fails CLOSED.
  - `tools/list` is filtered to the same set, so a restricted agent never even
    sees a capability it can't use. This is UX, and it fails OPEN — if a
    response shape is ever unrecognized it passes through unfiltered rather
    than breaking the connection, because call-time enforcement is what
    actually holds the line.

Installed INNER of `JWTAuthMiddleware` so `scope["user"]["claims"]` is
populated. Unauthenticated requests never reach it.
"""

from __future__ import annotations

import json
import logging
from typing import TYPE_CHECKING, Any

from starlette.types import ASGIApp, Message, Receive, Scope, Send

if TYPE_CHECKING:
    from homelab_mcp.config import Settings

log = logging.getLogger(__name__)
audit = logging.getLogger("homelab_mcp.audit")

# Cap the buffered request body. MCP JSON-RPC requests are small; anything
# larger is not a legitimate tools/call and shouldn't be parsed into memory.
_MAX_BODY = 1024 * 1024


def resolve_allowlist(
    claims: dict[str, Any] | None, restricted_scopes: dict[str, list[str]]
) -> set[str] | None:
    """Return the set of callable tool names, or None if unrestricted.

    A token is restricted when any whitespace-separated value of its `scope`
    claim matches a configured restricted scope. If several match, the union
    applies — a token holding two restricted scopes gets both allowlists,
    never more.
    """
    if not restricted_scopes or not claims:
        return None
    raw = claims.get("scope") or ""
    if not isinstance(raw, str):
        return None
    present = [s for s in raw.split() if s in restricted_scopes]
    if not present:
        return None
    allowed: set[str] = set()
    for name in present:
        allowed.update(restricted_scopes[name])
    return allowed


def _jsonrpc_error(request_id: Any, tool: str) -> dict[str, Any]:
    return {
        "jsonrpc": "2.0",
        "id": request_id,
        "error": {
            # -32601 "method not found" is the honest code: for this token the
            # tool genuinely does not exist as a callable method.
            "code": -32601,
            "message": (
                f"Tool {tool!r} is not available to this credential. This token is "
                "restricted to a subset of tools by its OAuth scope."
            ),
        },
    }


class ToolScopeMiddleware:
    """Restrict tools/call and tools/list for tokens carrying a restricted scope."""

    def __init__(self, app: ASGIApp, settings: Settings) -> None:
        self.app = app
        self.settings = settings
        self.mcp_path = "/" + settings.mcp_path.strip("/")

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or scope.get("method") != "POST":
            await self.app(scope, receive, send)
            return
        if scope.get("path", "").rstrip("/") != self.mcp_path:
            await self.app(scope, receive, send)
            return

        user = scope.get("user") or {}
        claims = user.get("claims") if isinstance(user, dict) else None
        allowed = resolve_allowlist(claims, self.settings.restricted_scopes)
        if allowed is None:
            await self.app(scope, receive, send)
            return

        body, more = b"", True
        messages: list[Message] = []
        while more:
            message = await receive()
            if message["type"] != "http.request":
                messages.append(message)
                break
            body += message.get("body", b"")
            more = message.get("more_body", False)
            if len(body) > _MAX_BODY:
                break

        blocked = self._blocked_call(body, allowed)
        if blocked is not None:
            request_id, tool = blocked
            email = user.get("email") if isinstance(user, dict) else None
            audit.warning(
                "tool_scope DENIED tool=%s user=%s allowed=%d",
                tool,
                email,
                len(allowed),
            )
            payload = json.dumps(_jsonrpc_error(request_id, tool)).encode()
            await send(
                {
                    "type": "http.response.start",
                    "status": 403,
                    "headers": [
                        (b"content-type", b"application/json"),
                        (b"content-length", str(len(payload)).encode()),
                    ],
                }
            )
            await send({"type": "http.response.body", "body": payload})
            return

        replayed = False

        async def _receive() -> Message:
            nonlocal replayed
            if not replayed:
                replayed = True
                return {"type": "http.request", "body": body, "more_body": False}
            if messages:
                return messages.pop(0)
            return await receive()

        await self._filtered_send(scope, _receive, send, allowed)

    @staticmethod
    def _blocked_call(body: bytes, allowed: set[str]) -> tuple[Any, str] | None:
        """Return (request_id, tool_name) if the body calls a disallowed tool."""
        try:
            parsed = json.loads(body or b"{}")
        except ValueError:
            # Not parseable as JSON — let the transport reject it properly.
            return None
        for req in parsed if isinstance(parsed, list) else [parsed]:
            if not isinstance(req, dict) or req.get("method") != "tools/call":
                continue
            params = req.get("params")
            name = params.get("name") if isinstance(params, dict) else None
            if isinstance(name, str) and name not in allowed:
                return req.get("id"), name
        return None

    async def _filtered_send(
        self, scope: Scope, receive: Receive, send: Send, allowed: set[str]
    ) -> None:
        """Pass the request through, filtering any tools/list result on the way out."""
        start: Message | None = None
        chunks: list[bytes] = []
        is_json = False

        async def _send(message: Message) -> None:
            nonlocal start, is_json
            if message["type"] == "http.response.start":
                start = message
                headers = {k.lower(): v for k, v in message.get("headers", [])}
                ctype = headers.get(b"content-type", b"")
                # Only buffer what we can safely rewrite. An SSE stream is left
                # alone: tools/call enforcement above is the real gate, and
                # stalling a stream to rewrite it risks breaking the transport.
                is_json = ctype.startswith(b"application/json")
                if not is_json:
                    await send(message)
                return

            if message["type"] != "http.response.body" or not is_json:
                await send(message)
                return

            chunks.append(message.get("body", b""))
            if message.get("more_body", False):
                return

            raw = b"".join(chunks)
            out = self._filter_tools_list(raw, allowed)
            assert start is not None
            # Rebuild Content-Length: filtering shortens the body, and a stale
            # length would truncate the response or hang the client.
            out_headers = [
                (k, v) for k, v in start.get("headers", []) if k.lower() != b"content-length"
            ]
            out_headers.append((b"content-length", str(len(out)).encode()))
            await send({**start, "headers": out_headers})
            await send({"type": "http.response.body", "body": out, "more_body": False})

        await self.app(scope, receive, _send)

    @staticmethod
    def _filter_tools_list(raw: bytes, allowed: set[str]) -> bytes:
        """Drop non-allowed entries from a tools/list result; pass anything else through."""
        try:
            parsed = json.loads(raw)
        except ValueError:
            return raw
        changed = False

        def _filter(obj: Any) -> Any:
            nonlocal changed
            if not isinstance(obj, dict):
                return obj
            result = obj.get("result")
            if isinstance(result, dict) and isinstance(result.get("tools"), list):
                kept = [
                    t
                    for t in result["tools"]
                    if not isinstance(t, dict) or t.get("name") in allowed
                ]
                if len(kept) != len(result["tools"]):
                    changed = True
                    result["tools"] = kept
            return obj

        parsed = [_filter(o) for o in parsed] if isinstance(parsed, list) else _filter(parsed)
        return json.dumps(parsed).encode() if changed else raw
