"""Shared HTTP plumbing for tool modules.

Every tool category wraps an upstream HTTP API and needs the same three
things: a stable structured error shape, a path-segment encoder that can't
be used for traversal, and one request helper that applies a timeout, maps
transport/HTTP failures to that error shape, and guards JSON decoding (an
SSO/proxy returning a 200 HTML page must not throw to the MCP transport).

Adopting this module keeps the error contract identical across cooklang,
grocy and gatus so an LLM client learns one shape:

    {"error": {"code": "...", "message": "...", "hint": "..."}}

Per AGENTS.md rule 4, tools NEVER raise to the transport — they catch
`ToolError` at the boundary and return `err.payload()`.
"""

from __future__ import annotations

import logging
from typing import Any
from urllib.parse import quote

import httpx

log = logging.getLogger(__name__)

# Default per-request timeout (seconds). Tools may override per call.
DEFAULT_TIMEOUT = 15.0


class ToolError(Exception):
    """An upstream/validation failure carrying a stable `code` for the client.

    Surfaced as a structured ``{"error": {code, message, hint}}`` payload —
    never raised to the MCP transport, and never carrying secrets or a raw
    upstream body.
    """

    def __init__(self, code: str, message: str, hint: str = "") -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.hint = hint

    def payload(self) -> dict[str, Any]:
        return {"error": {"code": self.code, "message": self.message, "hint": self.hint}}


def enc(segment: str) -> str:
    """URL-encode a single path segment, leaving no '/' to enable traversal."""
    return quote(segment, safe="")


def make_client(
    *,
    base_url: str = "",
    headers: dict[str, str] | None = None,
    timeout: float = DEFAULT_TIMEOUT,
) -> httpx.AsyncClient:
    """Construct a long-lived AsyncClient for a tool module.

    Create ONE per `register()` call and reuse it across invocations so
    connections (and TLS handshakes) are pooled instead of rebuilt per call.
    Constructing it outside the event loop is fine — httpx binds to the loop
    on first request.
    """
    return httpx.AsyncClient(base_url=base_url, headers=headers or {}, timeout=timeout)


async def request_json(
    client: httpx.AsyncClient,
    method: str,
    url: str,
    *,
    service: str,
    params: dict[str, Any] | None = None,
    json: Any | None = None,
    expect_json: bool = True,
    unreachable_hint: str = "",
) -> Any:
    """Perform a request and return decoded JSON, mapping failures to ToolError.

    `service` names the upstream for error codes/messages (e.g. "gatus"):
      - transport failure  -> ToolError("{service}_unreachable", ...)
      - HTTP >= 400        -> ToolError("{service}_http_{status}", <detail>)
      - non-JSON 2xx body  -> ToolError("{service}_http_{status}", "non-JSON …")

    Only the method + URL path and the exception class are logged — never
    params, bodies, headers, or auth.
    """
    try:
        resp = await client.request(method, url, params=params, json=json)
    except httpx.HTTPError as exc:
        log.warning("%s %s %s failed: %s", service, method, url, exc.__class__.__name__)
        raise ToolError(
            f"{service}_unreachable",
            f"Could not reach {service} ({exc.__class__.__name__}).",
            unreachable_hint,
        ) from exc

    if resp.status_code >= 400:
        detail: str | None = None
        try:
            body = resp.json()
            if isinstance(body, dict):
                detail = body.get("error_message") or body.get("error") or body.get("message")
        except ValueError:
            detail = (resp.text or "")[:200] or None
        raise ToolError(
            f"{service}_http_{resp.status_code}",
            str(detail) if detail else f"{service} returned HTTP {resp.status_code}.",
            "",
        )

    if not expect_json or resp.status_code == 204 or not resp.content:
        return None
    try:
        return resp.json()
    except ValueError as exc:
        raise ToolError(
            f"{service}_http_{resp.status_code}",
            f"{service} returned a non-JSON response.",
            "",
        ) from exc
