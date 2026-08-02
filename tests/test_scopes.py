"""Tool-allowlisting tests.

The acceptance requirement for the hermes credential is precise: it can call
exactly its four tools and is rejected from every other one. These tests hold
that line, including the cases that would quietly widen it — a batched
JSON-RPC request smuggling a forbidden call alongside a permitted one, and an
unrestricted token that must keep full access.
"""

from __future__ import annotations

import json
from typing import Any

import pytest
from starlette.applications import Starlette
from starlette.responses import JSONResponse, StreamingResponse
from starlette.routing import Route
from starlette.testclient import TestClient

from homelab_mcp.config import Settings
from homelab_mcp.scopes import (
    ToolScopeMiddleware,
    resolve_allowlist,
    resolve_resource_prefixes,
)

HERMES_TOOLS = {
    "finances_sync_status",
    "finances_monthly_summary",
    "finances_recurring",
    "finances_debt_status",
}


def _settings() -> Settings:
    return Settings(_env_file=None, oauth_required=False)  # type: ignore[call-arg]


# ── allowlist resolution ─────────────────────────────────────────────


def test_hermes_scope_resolves_to_exactly_four_tools() -> None:
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


def _app(scope_claim: str | None, *, sse_tools_list: bool = False) -> TestClient:
    """An app that echoes a tools/list result, wrapped in the scope middleware."""

    async def mcp_endpoint(request: Any) -> JSONResponse | StreamingResponse:
        body = await request.json()
        if body.get("method") == "tools/list":
            payload = {
                "jsonrpc": "2.0",
                "id": body.get("id"),
                "result": {
                    "tools": [
                        {"name": n}
                        for n in sorted(HERMES_TOOLS | {"finances_trend", "ha_call_service"})
                    ]
                },
            }
            if sse_tools_list:
                return StreamingResponse(
                    iter([f"data: {json.dumps(payload)}\n\n"]),
                    media_type="text/event-stream",
                )
            return JSONResponse(payload)
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
def test_hermes_can_call_each_of_its_four_tools(tool: str) -> None:
    resp = _call(_app("hermes"), tool)
    assert resp.status_code == 200
    assert resp.json()["result"] == {"ok": True}


@pytest.mark.parametrize(
    "tool",
    [
        "finances_trend",  # raw-ish series: deliberately out of remit
        "signal_send",  # Hermes delivers through its native Signal gateway
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
                "params": {"name": "finances_sync_status"},
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


def test_sse_tools_list_is_filtered_for_hermes() -> None:
    client = _app("hermes", sse_tools_list=True)
    resp = client.post("/mcp", json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("text/event-stream")
    data = next(
        line.removeprefix("data: ") for line in resp.text.splitlines() if line.startswith("data:")
    )
    names = {tool["name"] for tool in json.loads(data)["result"]["tools"]}
    assert names == HERMES_TOOLS


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
    resp = _call(client, "finances_sync_status")
    assert json.loads(resp.content)["result"] == {"ok": True}


# ── advisor scope (the interactive write layer) ──────────────────────

# The governance-doc layer: reads plus the two append tools. Advisor only —
# hermes's context is baked into its persona and it composes from numbers.
# The transaction-context store. hermes may SCRIBE and READ here — its only
# writable surface anywhere — but may not consume, because consuming asserts
# that a human-judged categorization already happened in the ledger.
CONTEXT_SHARED = {
    "finances_context_add",
    "finances_context_list",
    "finances_clarify_candidates",
}
CONTEXT_ADVISOR_ONLY = {"finances_context_consume"}

ADVISOR_DOCS = {
    "finances_docs_get",
    "finances_decision_append",
    "finances_planned_append",
}

ADVISOR_ADDED_V2 = {
    "finances_payees",
    "finances_payee_merge",
    "finances_buffer",
    "finances_breaches",
    "finances_room",
    "finances_reconcile",
    "finances_subscriptions",
    "finances_net_worth",
    "finances_payoff_projection",
}
# hermes gains exactly these three and nothing else.
HERMES_ADDED_V2 = {"finances_buffer", "finances_breaches", "finances_room"}

ADVISOR_TOOLS = {
    "finances_sync_status",
    "finances_monthly_summary",
    "finances_recurring",
    "finances_trend",
    "finances_debt_status",
    "finances_transactions",
    "finances_categorize",
    "finances_rules_list",
    "finances_rule_create",
    "finances_rule_delete",
    "paperless_search",
    "paperless_get",
    "paperless_link",
    "signal_send",
    *ADVISOR_ADDED_V2,
    *ADVISOR_DOCS,
    *CONTEXT_SHARED,
    *CONTEXT_ADVISOR_ONLY,
}

# Everything the advisor layer added. hermes must reach none of it.
NEW_WRITE_TOOLS = [
    "finances_transactions",
    "finances_categorize",
    "finances_rules_list",
    "finances_rule_create",
    "finances_rule_delete",
]


def test_advisor_scope_resolves_to_the_financial_surface() -> None:
    allowed = resolve_allowlist({"scope": "advisor"}, _settings().restricted_scopes)
    assert allowed == ADVISOR_TOOLS


def test_advisor_scope_excludes_the_physical_control_plane() -> None:
    """Narrowing, not granting: an advisor token cannot actuate the house."""
    allowed = resolve_allowlist({"scope": "advisor"}, _settings().restricted_scopes)
    assert allowed is not None
    for tool in ("ha_call_service", "cooklang_delete_recipe", "grocy_stock_item"):
        assert tool not in allowed


HERMES_TOOLS = {
    "finances_sync_status",
    "finances_monthly_summary",
    "finances_recurring",
    "finances_debt_status",
    *HERMES_ADDED_V2,
    *CONTEXT_SHARED,
}


def test_hermes_scope_is_exactly_its_ten() -> None:
    """Seven reads plus the three context tools it may scribe/read with."""
    allowed = resolve_allowlist({"scope": "hermes"}, _settings().restricted_scopes)
    assert allowed == HERMES_TOOLS
    assert len(allowed) == 10


@pytest.mark.parametrize(
    "tool",
    sorted(
        (ADVISOR_ADDED_V2 | ADVISOR_DOCS | CONTEXT_ADVISOR_ONLY) - HERMES_ADDED_V2 - CONTEXT_SHARED
    ),
)
def test_hermes_is_403_on_every_advisor_only_tool(tool: str) -> None:
    """The balance sheet, the payee writer and the raw scans stay out of reach."""
    resp = _call(_app("hermes"), tool)
    assert resp.status_code == 403
    assert resp.json()["error"]["code"] == -32601


@pytest.mark.parametrize("tool", sorted(HERMES_ADDED_V2))
def test_hermes_can_call_its_three_new_tools(tool: str) -> None:
    assert _call(_app("hermes"), tool).status_code == 200


def test_advisor_is_a_superset_of_hermes() -> None:
    scopes = _settings().restricted_scopes
    advisor = resolve_allowlist({"scope": "advisor"}, scopes)
    hermes = resolve_allowlist({"scope": "hermes"}, scopes)
    assert advisor is not None and hermes is not None
    assert hermes <= advisor


@pytest.mark.parametrize("tool", NEW_WRITE_TOOLS)
def test_hermes_is_403_on_every_new_advisor_tool(tool: str) -> None:
    """The unattended agent must not reach raw transactions or any writer."""
    resp = _call(_app("hermes"), tool)
    assert resp.status_code == 403
    assert resp.json()["error"]["code"] == -32601


@pytest.mark.parametrize("tool", sorted(ADVISOR_TOOLS))
def test_advisor_can_call_each_of_its_tools(tool: str) -> None:
    resp = _call(_app("advisor"), tool)
    assert resp.status_code == 200


def test_advisor_is_still_refused_outside_its_surface() -> None:
    for tool in ("ha_call_service", "arc_log_raid", "cooklang_create_recipe"):
        assert _call(_app("advisor"), tool).status_code == 403


def test_hermes_reads_still_work_alongside_the_new_scope() -> None:
    """Adding a scope must not disturb the existing one."""
    for tool in (
        "finances_sync_status",
        "finances_monthly_summary",
        "finances_recurring",
        "finances_debt_status",
    ):
        assert _call(_app("hermes"), tool).status_code == 200


# ── resource scoping ─────────────────────────────────────────────────
# Resources are URI-addressed, so they need their own gate. Before this the
# middleware ignored resources/* entirely and any authenticated token could
# have read every governance document.


def test_advisor_resolves_the_finances_prefix() -> None:
    got = resolve_resource_prefixes({"scope": "advisor"}, _settings().restricted_scope_resources)
    assert got == ["finances://"]


def test_hermes_resolves_no_resource_prefixes() -> None:
    """Fail closed: a scope with no entry gets nothing, not everything."""
    got = resolve_resource_prefixes({"scope": "hermes"}, _settings().restricted_scope_resources)
    assert got is None  # no entry -> the middleware substitutes an empty list


def test_unrestricted_token_is_unaffected() -> None:
    assert (
        resolve_resource_prefixes({"scope": "openid"}, _settings().restricted_scope_resources)
        is None
    )


def _read(client: TestClient, uri: str) -> Any:
    return client.post(
        "/mcp",
        json={"jsonrpc": "2.0", "id": 1, "method": "resources/read", "params": {"uri": uri}},
    )


def test_advisor_may_read_a_finances_resource() -> None:
    assert _read(_app("advisor"), "finances://PLAN.md").status_code == 200


@pytest.mark.parametrize(
    "uri",
    ["finances://PLAN.md", "finances://DECISIONS.md", "finances://PLANNED.md"],
)
def test_hermes_is_403_on_every_finances_resource(uri: str) -> None:
    resp = _read(_app("hermes"), uri)
    assert resp.status_code == 403
    assert resp.json()["error"]["code"] == -32601


def test_restricted_token_cannot_reach_an_unlisted_scheme() -> None:
    """Even advisor is confined to its granted prefixes."""
    assert _read(_app("advisor"), "file:///etc/passwd").status_code == 403
    assert _read(_app("advisor"), "secrets://token").status_code == 403


def test_resource_catalog_is_filtered_for_hermes() -> None:
    """hermes must not even see that the documents exist."""

    async def endpoint(request: Any) -> JSONResponse:
        body = await request.json()
        return JSONResponse(
            {
                "jsonrpc": "2.0",
                "id": body.get("id"),
                "result": {
                    "resources": [
                        {"uri": "finances://PLAN.md", "name": "PLAN.md"},
                        {"uri": "finances://DECISIONS.md", "name": "DECISIONS.md"},
                    ]
                },
            }
        )

    app = Starlette(routes=[Route("/mcp", endpoint, methods=["POST"])])

    class Inject:
        def __init__(self, inner: Any) -> None:
            self.inner = inner

        async def __call__(self, scope: Any, receive: Any, send: Any) -> None:
            if scope["type"] == "http":
                scope["user"] = {"email": "h@x", "claims": {"scope": "hermes"}}
            await self.inner(scope, receive, send)

    app.add_middleware(ToolScopeMiddleware, settings=_settings())
    app.add_middleware(Inject)
    resp = TestClient(app).post(
        "/mcp", json={"jsonrpc": "2.0", "id": 1, "method": "resources/list"}
    )
    assert resp.json()["result"]["resources"] == []
    assert int(resp.headers["content-length"]) == len(resp.content)


# ── transaction-context scoping ──────────────────────────────────────


def test_hermes_may_scribe_and_read_context() -> None:
    """Recording what someone replied is hermes's one writable capability."""
    for tool in sorted(CONTEXT_SHARED):
        assert _call(_app("hermes"), tool).status_code == 200


def test_hermes_may_not_consume_context() -> None:
    """Consuming asserts a human-judged categorization already happened.

    hermes never touches the ledger, so it can never be the one to say so.
    """
    resp = _call(_app("hermes"), "finances_context_consume")
    assert resp.status_code == 403
    assert resp.json()["error"]["code"] == -32601


def test_advisor_may_consume_context() -> None:
    assert _call(_app("advisor"), "finances_context_consume").status_code == 200


def test_context_tools_do_not_grant_ledger_writes_to_hermes() -> None:
    """The store is not a side door into Actual."""
    for tool in ("finances_categorize", "finances_rule_create", "finances_payee_merge"):
        assert _call(_app("hermes"), tool).status_code == 403
