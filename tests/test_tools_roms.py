"""ROM-library tool tests (FileBrowser Quantum).

Exercises the login handshake + token caching, the 401 re-login retry,
path validation (AGENTS rule 3), the projection/truncation contract, and
the structured-error contract (no raise to the transport).
"""

from __future__ import annotations

import base64
import json
from collections.abc import Callable
from typing import Any

import pytest
from pytest_httpx import HTTPXMock

from homelab_mcp.config import Settings
from homelab_mcp.tools.roms import register

BASE = "http://roms.test"
LOGIN_URL = f"{BASE}/api/auth/login?username=ryan&recaptcha="

pytestmark = pytest.mark.httpx_mock(assert_all_responses_were_requested=False)


def _fake_jwt(exp: float = 9999999999.0) -> str:
    payload = base64.urlsafe_b64encode(json.dumps({"exp": exp}).encode()).decode().rstrip("=")
    return f"h.{payload}.sig"


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
    monkeypatch.setenv("HOMELAB_MCP_ROMS_BASE_URL", BASE)
    monkeypatch.setenv("HOMELAB_MCP_ROMS_USERNAME", "ryan")
    monkeypatch.setenv("HOMELAB_MCP_ROMS_PASSWORD", "hunter2")
    mcp = CapturingMCP()
    register(mcp, Settings())  # type: ignore[arg-type]
    return mcp.tools


def _mock_login(httpx_mock: HTTPXMock, token: str | None = None) -> None:
    httpx_mock.add_response(method="POST", url=LOGIN_URL, text=token or _fake_jwt())


async def test_list_systems_projects_folders(
    tools: dict[str, Callable[..., Any]], httpx_mock: HTTPXMock
) -> None:
    _mock_login(httpx_mock)
    httpx_mock.add_response(
        url=f"{BASE}/api/resources?path=%2F&source=share",
        json={
            "type": "directory",
            "folders": [
                {"name": "snes", "size": 3580334080, "modified": "2026-07-27T18:08:14Z"},
                {"name": "n64", "size": 5093810176, "modified": "2026-07-27T18:18:30Z"},
            ],
        },
    )
    out = await tools["roms_list_systems"]()
    assert out["total"] == 2
    assert out["systems"][0] == {
        "system": "snes",
        "size_bytes": 3580334080,
        "modified": "2026-07-27T18:08:14Z",
    }


async def test_token_is_cached_across_calls(
    tools: dict[str, Callable[..., Any]], httpx_mock: HTTPXMock
) -> None:
    """Two tool calls must reuse one session token (one login POST total)."""
    _mock_login(httpx_mock)
    httpx_mock.add_response(
        url=f"{BASE}/api/resources?path=%2F&source=share",
        json={"type": "directory", "folders": []},
        is_reusable=True,
    )
    await tools["roms_list_systems"]()
    await tools["roms_list_systems"]()
    logins = [r for r in httpx_mock.get_requests() if r.url.path == "/api/auth/login"]
    assert len(logins) == 1
    api_calls = [r for r in httpx_mock.get_requests() if r.url.path == "/api/resources"]
    assert all(r.headers["Authorization"].startswith("Bearer ") for r in api_calls)


async def test_rejected_token_triggers_one_relogin(
    tools: dict[str, Callable[..., Any]], httpx_mock: HTTPXMock
) -> None:
    """A 401 on an API call re-logs-in once and retries, then succeeds."""
    _mock_login(httpx_mock)
    httpx_mock.add_response(
        url=f"{BASE}/api/resources?path=%2F&source=share",
        status_code=401,
        json={"status": 401, "message": "no token present in request"},
    )
    _mock_login(httpx_mock)
    httpx_mock.add_response(
        url=f"{BASE}/api/resources?path=%2F&source=share",
        json={"type": "directory", "folders": [{"name": "wii", "size": 1}]},
    )
    out = await tools["roms_list_systems"]()
    assert out["total"] == 1
    logins = [r for r in httpx_mock.get_requests() if r.url.path == "/api/auth/login"]
    assert len(logins) == 2


async def test_login_failure_is_structured_not_raised(
    tools: dict[str, Callable[..., Any]], httpx_mock: HTTPXMock
) -> None:
    httpx_mock.add_response(
        method="POST", url=LOGIN_URL, status_code=401, json={"message": "user unauthorized"}
    )
    out = await tools["roms_list_systems"]()
    assert out["error"]["code"] == "roms_auth_failed"


async def test_browse_rejects_traversal_before_any_http(
    tools: dict[str, Callable[..., Any]], httpx_mock: HTTPXMock
) -> None:
    out = await tools["roms_browse"](path="/snes/../../etc")
    assert out["error"]["code"] == "roms_invalid_path"
    assert len(httpx_mock.get_requests()) == 0


async def test_browse_projects_files_with_parsed_names(
    tools: dict[str, Callable[..., Any]], httpx_mock: HTTPXMock
) -> None:
    _mock_login(httpx_mock)
    httpx_mock.add_response(
        url=f"{BASE}/api/resources?path=%2Fsnes%2F&source=share",
        json={
            "type": "directory",
            "folders": [{"name": "SNES Japanese ROMs", "size": 5, "modified": "x"}],
            "files": [
                {
                    "name": "Final Fantasy III (USA) (Rev 1).sfc",
                    "size": 3145728,
                    "type": "application/vnd.nintendo.snes.rom",
                    "modified": "2025-04-15T03:44:28Z",
                }
            ],
        },
    )
    out = await tools["roms_browse"](path="/snes/")
    assert out["folders"][0]["name"] == "SNES Japanese ROMs"
    f = out["files"][0]
    assert f["title"] == "Final Fantasy III"
    assert f["tags"] == ["USA", "Rev 1"]
    assert f["extension"] == "sfc"
    assert out["truncated"] is False


async def test_browse_truncation_is_flagged(
    tools: dict[str, Callable[..., Any]], httpx_mock: HTTPXMock
) -> None:
    _mock_login(httpx_mock)
    files = [{"name": f"Game {i}.sfc", "size": 1, "type": "t", "modified": "m"} for i in range(5)]
    httpx_mock.add_response(
        url=f"{BASE}/api/resources?path=%2Fsnes%2F&source=share",
        json={"type": "directory", "folders": [], "files": files},
    )
    out = await tools["roms_browse"](path="/snes/", limit=2)
    assert out["returned"] == 2
    assert out["total"] == 5
    assert out["truncated"] is True


async def test_search_scopes_to_system_and_extracts_fields(
    tools: dict[str, Callable[..., Any]], httpx_mock: HTTPXMock
) -> None:
    _mock_login(httpx_mock)
    httpx_mock.add_response(
        url=f"{BASE}/api/tools/search?query=mario&sources=share&scope=%2Fsnes%2F",
        json=[
            {
                "path": "snes/Super Mario World (USA).sfc",
                "type": "application/vnd.nintendo.snes.rom",
                "size": 524288,
                "modified": "2025-04-15T03:44:23Z",
                "source": "share",
            }
        ],
    )
    out = await tools["roms_search"](query="mario", system="snes")
    hit = out["results"][0]
    assert hit["path"] == "/snes/Super Mario World (USA).sfc"
    assert hit["system"] == "snes"
    assert hit["title"] == "Super Mario World"
    assert hit["tags"] == ["USA"]
    assert out["truncated"] is False


async def test_search_rejects_bad_system_name(
    tools: dict[str, Callable[..., Any]], httpx_mock: HTTPXMock
) -> None:
    out = await tools["roms_search"](query="mario", system="../etc")
    assert out["error"]["code"] == "roms_invalid_system"
    assert len(httpx_mock.get_requests()) == 0


async def test_get_download_url_builds_encoded_url(
    tools: dict[str, Callable[..., Any]], httpx_mock: HTTPXMock
) -> None:
    _mock_login(httpx_mock)
    httpx_mock.add_response(
        url=f"{BASE}/api/resources?path=%2Fsnes%2FSecret+of+Mana+%28USA%29.sfc&source=share",
        json={
            "name": "Secret of Mana (USA).sfc",
            "type": "application/vnd.nintendo.snes.rom",
            "size": 2097152,
            "modified": "2025-04-15T03:44:33Z",
        },
    )
    out = await tools["roms_get_download_url"](path="/snes/Secret of Mana (USA).sfc")
    assert out["size_bytes"] == 2097152
    assert out["download_url"].startswith(f"{BASE}/api/resources/download?")
    assert "file=%2Fsnes%2FSecret+of+Mana+%28USA%29.sfc" in out["download_url"]
    assert "source=share" in out["download_url"]


async def test_get_download_url_refuses_directory(
    tools: dict[str, Callable[..., Any]], httpx_mock: HTTPXMock
) -> None:
    _mock_login(httpx_mock)
    httpx_mock.add_response(
        url=f"{BASE}/api/resources?path=%2Fsnes%2F&source=share",
        json={"type": "directory", "folders": [], "files": []},
    )
    out = await tools["roms_get_download_url"](path="/snes/")
    assert out["error"]["code"] == "roms_not_a_file"


async def test_unconfigured_returns_config_error(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HOMELAB_MCP_OAUTH_REQUIRED", "false")
    monkeypatch.delenv("HOMELAB_MCP_ROMS_BASE_URL", raising=False)
    monkeypatch.delenv("HOMELAB_MCP_ROMS_PASSWORD", raising=False)
    mcp = CapturingMCP()
    register(mcp, Settings())  # type: ignore[arg-type]
    out = await mcp.tools["roms_list_systems"]()
    assert out["error"]["code"] == "roms_unreachable"
    assert "HOMELAB_MCP_ROMS_BASE_URL" in out["error"]["hint"]
