"""Integration tests for the protected-resource metadata endpoint + allowlist behavior."""

from __future__ import annotations

import pytest
from starlette.testclient import TestClient

from homelab_mcp.app import build_app
from homelab_mcp.config import Settings


@pytest.fixture(autouse=True)
def _set_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HOMELAB_MCP_CF_ACCESS_TEAM", "testteam")
    monkeypatch.setenv("HOMELAB_MCP_CF_ACCESS_APP_ID", "deadbeef" * 8)
    monkeypatch.setenv("HOMELAB_MCP_PUBLIC_BASE_URL", "https://mcp.example.com")


def test_prm_endpoint_returns_well_formed_metadata() -> None:
    """RFC 9728 metadata endpoint must be reachable WITHOUT auth and JSON-shaped."""
    settings = Settings()
    app = build_app(settings)

    with TestClient(app) as client:
        # Crucially: no Authorization header.
        resp = client.get("/.well-known/oauth-protected-resource")

    assert resp.status_code == 200, (
        f"PRM endpoint should be unauthenticated; got {resp.status_code}"
    )
    body = resp.json()

    # Required fields per RFC 9728.
    assert body["resource"] == "https://mcp.example.com"
    assert body["authorization_servers"] == [
        f"https://testteam.cloudflareaccess.com/cdn-cgi/access/sso/oidc/{'deadbeef' * 8}"
    ]
    # Informational helpful fields.
    assert "bearer_methods_supported" in body
    assert body["bearer_methods_supported"] == ["header"]


def test_mcp_endpoint_still_requires_auth() -> None:
    """The PRM allowlist must not leak through to /mcp."""
    settings = Settings()
    app = build_app(settings)

    with TestClient(app) as client:
        resp = client.post(
            "/mcp",
            headers={
                "Accept": "application/json, text/event-stream",
                "Content-Type": "application/json",
            },
            json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
        )

    assert resp.status_code == 401
    body = resp.json()
    assert body["error"] == "unauthorized"


def test_arbitrary_well_known_path_not_allowlisted() -> None:
    """Allowlist is exact-match; siblings under /.well-known/ should still be challenged."""
    settings = Settings()
    app = build_app(settings)

    with TestClient(app) as client:
        # Not in UNAUTHENTICATED_PATHS — should hit the JWT middleware and 401.
        resp = client.get("/.well-known/openid-configuration")

    assert resp.status_code == 401
