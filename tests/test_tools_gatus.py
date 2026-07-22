"""Gatus uptime-tool tests.

Exercises the projection logic, the structured-error contract (no raise to
the transport, even on a 5xx or non-JSON body), and path-segment encoding
of the client-supplied endpoint key.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

import pytest
from pytest_httpx import HTTPXMock

from homelab_mcp.config import Settings
from homelab_mcp.tools.gatus import register

BASE = "https://gatus.test"

pytestmark = pytest.mark.httpx_mock(assert_all_responses_were_requested=False)


class CapturingMCP:
    """Collects tools registered via @mcp.tool(name=...) so tests can call them."""

    def __init__(self) -> None:
        self.tools: dict[str, Callable[..., Any]] = {}

    def tool(
        self, *, name: str, description: str = "", annotations: Any = None
    ) -> Callable[..., Any]:
        def deco(fn: Callable[..., Any]) -> Callable[..., Any]:
            self.tools[name] = fn
            return fn

        return deco


@pytest.fixture
def tools(monkeypatch: pytest.MonkeyPatch) -> dict[str, Callable[..., Any]]:
    monkeypatch.setenv("HOMELAB_MCP_OAUTH_REQUIRED", "false")
    monkeypatch.setenv("HOMELAB_MCP_GATUS_BASE_URL", BASE)
    mcp = CapturingMCP()
    register(mcp, Settings())  # type: ignore[arg-type]
    return mcp.tools


async def test_list_status_projects_summary(
    tools: dict[str, Callable[..., Any]], httpx_mock: HTTPXMock
) -> None:
    httpx_mock.add_response(
        url=f"{BASE}/api/v1/endpoints/statuses",
        json=[
            {
                "group": "core",
                "name": "router",
                "key": "core_router",
                "results": [{"success": True, "timestamp": "2026-01-01T00:00:00Z"}],
                "uptime": {"7d": 0.99, "24h": 1.0},
            },
            {
                "group": "core",
                "name": "nas",
                "key": "core_nas",
                "results": [{"success": False, "timestamp": "2026-01-01T00:05:00Z"}],
                "uptime": {"7d": 0.5},
            },
        ],
    )
    out = await tools["homelab_list_status"]()
    assert out["total"] == 2
    assert out["up"] == 1
    assert out["down"] == 1
    assert out["endpoints"][0]["current_status"] == "up"


async def test_list_status_error_is_structured_not_raised(
    tools: dict[str, Callable[..., Any]], httpx_mock: HTTPXMock
) -> None:
    """A 503 upstream must return an {error:...} payload, not raise (AGENTS rule 4)."""
    httpx_mock.add_response(url=f"{BASE}/api/v1/endpoints/statuses", status_code=503)
    out = await tools["homelab_list_status"]()
    assert "error" in out
    assert out["error"]["code"] == "gatus_http_503"


async def test_history_encodes_key_path_segment(
    tools: dict[str, Callable[..., Any]], httpx_mock: HTTPXMock
) -> None:
    """A traversal-y key must be percent-encoded into a single path segment."""
    httpx_mock.add_response(
        url=f"{BASE}/api/v1/endpoints/..%2F..%2Fadmin/statuses?page=1&pageSize=20",
        json=[{"success": True}],
    )
    out = await tools["homelab_get_endpoint_history"](key="../../admin")
    assert out["key"] == "../../admin"
    assert out["results"] == [{"success": True}]


async def test_history_non_json_is_structured_error(
    tools: dict[str, Callable[..., Any]], httpx_mock: HTTPXMock
) -> None:
    """A 200 HTML page (SSO/proxy) must not throw a JSON decode error."""
    httpx_mock.add_response(
        url=f"{BASE}/api/v1/endpoints/core_router/statuses?page=1&pageSize=20",
        text="<html>login</html>",
        headers={"content-type": "text/html"},
    )
    out = await tools["homelab_get_endpoint_history"](key="core_router")
    assert "error" in out
    assert out["error"]["code"].startswith("gatus_http_")
