"""Tool-allowlisting tests.

The acceptance requirement for the hermes credential is precise: it can call
exactly its five tools and is rejected from every other one. These tests hold
that line, including the cases that would quietly widen it — a batched
JSON-RPC request smuggling a forbidden call alongside a permitted one, and an
unrestricted token that must keep full access.
"""

from __future__ import annotations

import json
from typing import Any

import pytest
from starlette.applications import Starlette
from starlette.responses import JSONResponse
from starlette.routing import Route
from starlette.testclient import TestClient

from homelab_mcp.config import Settings
from homelab_mcp.scopes import ToolScopeMiddleware, resolve_allowlist

HERMES_TOOLS = {
    "finances_sync_status",
    "finances_monthly_summary",
    "finances_recurring",
    "finances_debt_status",
    "signal_send",
}


def _settings() -> Settings:
    return Settings(_env_file=None, oauth_required=False)  # type: ignore[call-arg]


# ── allowlist resolution ─────────────────────────────────────────────


def test_hermes_scope_resolves_to_exactly_five_tools() -> None:
    allowed = resolve_allowlist({"scope": "hermes"}, _settings().restricted_scopes)
    assert allowed == HERMES_TOOLS


def test_no_scope_claim_is_unrestricted() -> None:
    # Interactive advisor sessions keep everything.
    assert resolve_allowlist({"scope": ""}, _settings().restricted_scopes) is None
    assert resolve_allowlist({}, _settings().restricted_scopes) is None
    assert resolve_allowlist(None, _settings().restricted_scopes) is None


def test_unknown_scope_is_unrestricted_not_empty() -> None:
    """An unrecognized scope must not silently mean 'no tools at all'."""
    assert (
        resolve_allowlist({"scope": "openid email profile"}, _settings().restricted_scopes) is None
    )


def test_scope_is_matched_per_token_not_by_substring() -> None:
    """'hermes-ish' must not inherit hermes's allowlist."""
    assert resolve_allowlist({"scope": "hermesish"}, _settings().restricted_scopes) is None
    # But a space-separated list containing it does match.
    assert (
        resolve_allowlist({"scope": "openid hermes"}, _settings().restricted_scopes) == HERMES_TOOLS
    )


# ── dispatch enforcement ─────────────────────────────────────────────


def _app(scope_claim: str | None) -> TestClient:
    """An app that echoes a tools/list result, wrapped in the scope middleware."""

    async def mcp_endpoint(request: Any) -> JSONResponse:
        body = await request.json()
        if body.get("method") == "tools/list":
            return JSONResponse(
                {
                    "jsonrpc": "2.0",
                    "id": body.get("id"),
                    "result": {
                        "tools": [
                            {"name": n}
                            for n in sorted(HERMES_TOOLS | {"finances_trend", "ha_call_service"})
                        ]
                    },
                }
            )
        return JSONResponse({"jsonrpc": "2.0", "id": body.get("id"), "result": {"ok": True}})

    app = Starlette(routes=[Route("/mcp", mcp_endpoint, methods=["POST"])])

    class InjectUser:
        def __init__(self, inner: Any) -> None:
            self.inner = inner

        async def __call__(self, scope: Any, receive: Any, send: Any) -> None:
            if scope["type"] == "http" and scope_claim is not None:
                scope["user"] = {
                    "email": "hermes@holthome.net",
                    "claims": {"scope": scope_claim},
                }
            await self.inner(scope, receive, send)

    app.add_middleware(ToolScopeMiddleware, settings=_settings())
    app.add_middleware(InjectUser)
    return TestClient(app)


def _call(client: TestClient, tool: str) -> Any:
    return client.post(
        "/mcp",
        json={"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": tool}},
    )


@pytest.mark.parametrize("tool", sorted(HERMES_TOOLS))
def test_hermes_can_call_each_of_its_five_tools(tool: str) -> None:
    resp = _call(_app("hermes"), tool)
    assert resp.status_code == 200
    assert resp.json()["result"] == {"ok": True}


@pytest.mark.parametrize(
    "tool",
    [
        "finances_trend",  # raw-ish series: deliberately out of remit
        "paperless_search",
        "paperless_link",
        "ha_call_service",  # the physical control plane
        "cooklang_delete_recipe",
        "grocy_ensure",
    ],
)
def test_hermes_is_rejected_from_everything_else(tool: str) -> None:
    resp = _call(_app("hermes"), tool)
    assert resp.status_code == 403
    assert resp.json()["error"]["code"] == -32601


def test_unrestricted_token_can_call_anything() -> None:
    resp = _call(_app("openid email"), "ha_call_service")
    assert resp.status_code == 200


def test_batched_request_is_blocked_if_any_call_is_forbidden() -> None:
    """A permitted call must not act as a carrier for a forbidden one."""
    client = _app("hermes")
    resp = client.post(
        "/mcp",
        json=[
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "tools/call",
                "params": {"name": "signal_send"},
            },
            {
                "jsonrpc": "2.0",
                "id": 2,
                "method": "tools/call",
                "params": {"name": "ha_call_service"},
            },
        ],
    )
    assert resp.status_code == 403


def test_tools_list_is_filtered_for_hermes() -> None:
    client = _app("hermes")
    resp = client.post("/mcp", json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
    assert resp.status_code == 200
    names = {t["name"] for t in resp.json()["result"]["tools"]}
    assert names == HERMES_TOOLS
    # Content-Length must be rewritten to the filtered body, or the client hangs.
    assert int(resp.headers["content-length"]) == len(resp.content)


def test_tools_list_is_unfiltered_for_an_unrestricted_token() -> None:
    client = _app("openid")
    resp = client.post("/mcp", json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
    names = {t["name"] for t in resp.json()["result"]["tools"]}
    assert "ha_call_service" in names and "finances_trend" in names


def test_non_mcp_paths_are_untouched() -> None:
    """The middleware must not interfere with OAuth or health endpoints."""

    async def other(_request: Any) -> JSONResponse:
        return JSONResponse({"ok": True})

    app = Starlette(routes=[Route("/healthz", other, methods=["POST"])])
    app.add_middleware(ToolScopeMiddleware, settings=_settings())
    assert TestClient(app).post("/healthz", json={}).json() == {"ok": True}


def test_malformed_body_is_passed_through_not_swallowed() -> None:
    """Let the transport produce its own error for garbage input.

    The middleware must forward an unparseable body to the app rather than
    inventing a scope decision about it. `raise_server_exceptions=False` lets
    the downstream failure surface as a 500 so we can tell the two apart.
    """
    inner = _app("hermes").app
    client = TestClient(inner, raise_server_exceptions=False)
    resp = client.post("/mcp", content=b"{not json", headers={"content-type": "application/json"})
    # A 500 from the app is fine; a 403 would mean we masked a parse error.
    assert resp.status_code != 403


def test_denied_call_is_audit_logged(caplog: pytest.LogCaptureFixture) -> None:
    with caplog.at_level("WARNING", logger="homelab_mcp.audit"):
        _call(_app("hermes"), "ha_call_service")
    assert any("tool_scope DENIED" in r.getMessage() for r in caplog.records)


def test_json_body_without_tools_list_result_passes_through_unchanged() -> None:
    """Fail-open on shapes we don't recognize; call-time enforcement is the gate."""
    client = _app("hermes")
    resp = _call(client, "signal_send")
    assert json.loads(resp.content)["result"] == {"ok": True}
