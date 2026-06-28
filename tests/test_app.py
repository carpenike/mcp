"""Integration tests for the OAuth + discovery + auth-allowlist wiring."""

from __future__ import annotations

import pytest
from starlette.testclient import TestClient

from homelab_mcp.app import build_app
from homelab_mcp.config import Settings


@pytest.fixture
def settings(monkeypatch: pytest.MonkeyPatch, tmp_path) -> Settings:
    monkeypatch.setenv("HOMELAB_MCP_PUBLIC_BASE_URL", "https://mcp.example.com")
    monkeypatch.setenv("HOMELAB_MCP_POCKETID_ISSUER", "https://id.example.com")
    monkeypatch.setenv("HOMELAB_MCP_POCKETID_CLIENT_ID", "mcp-client")
    monkeypatch.setenv("HOMELAB_MCP_POCKETID_CLIENT_SECRET", "shh")
    # Put the auto-generated signing key in a tmpdir, not /var/lib.
    monkeypatch.setenv("HOMELAB_MCP_OAUTH_SIGNING_KEY_PATH", str(tmp_path / "signing-key.pem"))
    monkeypatch.setenv("HOMELAB_MCP_OAUTH_STATE_DB_PATH", str(tmp_path / "state.db"))
    return Settings()


def test_prm_endpoint_returns_well_formed_metadata(settings: Settings) -> None:
    """RFC 9728 metadata endpoint must be reachable WITHOUT auth, JSON-shaped, and point at us."""
    app = build_app(settings)
    with TestClient(app) as client:
        resp = client.get("/.well-known/oauth-protected-resource")

    assert resp.status_code == 200
    body = resp.json()
    assert body["resource"] == "https://mcp.example.com"
    # Self-hosted AS now: authorization_servers points at ourselves.
    assert body["authorization_servers"] == ["https://mcp.example.com"]
    assert body["bearer_methods_supported"] == ["header"]


def test_as_metadata_returns_spec_clean_fields(settings: Settings) -> None:
    """RFC 8414 doc must use exact spec field names — Claude is strict about this."""
    app = build_app(settings)
    with TestClient(app) as client:
        resp = client.get("/.well-known/oauth-authorization-server")

    assert resp.status_code == 200
    body = resp.json()
    assert body["issuer"] == "https://mcp.example.com"
    assert body["authorization_endpoint"] == "https://mcp.example.com/oauth/authorize"
    assert body["token_endpoint"] == "https://mcp.example.com/oauth/token"
    assert body["registration_endpoint"] == "https://mcp.example.com/oauth/register"
    assert body["jwks_uri"] == "https://mcp.example.com/oauth/jwks.json"
    # Spec-clean field names; this was the bug that drove the rewrite.
    assert body["response_types_supported"] == ["code"]
    assert body["grant_types_supported"] == ["authorization_code", "refresh_token"]
    assert body["code_challenge_methods_supported"] == ["S256"]
    assert "token_endpoint_auth_methods_supported" in body


def test_register_rate_limit_returns_429(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    """The unauthenticated DCR endpoint must cap registrations per IP."""
    monkeypatch.setenv("HOMELAB_MCP_PUBLIC_BASE_URL", "https://mcp.example.com")
    monkeypatch.setenv("HOMELAB_MCP_POCKETID_ISSUER", "https://id.example.com")
    monkeypatch.setenv("HOMELAB_MCP_POCKETID_CLIENT_ID", "mcp-client")
    monkeypatch.setenv("HOMELAB_MCP_POCKETID_CLIENT_SECRET", "shh")
    monkeypatch.setenv("HOMELAB_MCP_OAUTH_SIGNING_KEY_PATH", str(tmp_path / "signing-key.pem"))
    monkeypatch.setenv("HOMELAB_MCP_OAUTH_STATE_DB_PATH", str(tmp_path / "state.db"))
    monkeypatch.setenv("HOMELAB_MCP_OAUTH_REGISTER_RATE_LIMIT_MAX", "3")

    app = build_app(Settings())
    body = {
        "redirect_uris": ["https://claude.ai/api/mcp/auth_callback"],
        "client_name": "Claude",
        "token_endpoint_auth_method": "client_secret_post",
    }
    with TestClient(app) as client:
        # First 3 within the window succeed (201); the 4th is capped (429).
        for _ in range(3):
            assert client.post("/oauth/register", json=body).status_code == 201
        limited = client.post("/oauth/register", json=body)
    assert limited.status_code == 429
    assert limited.json()["error"] == "temporarily_unavailable"


def test_jwks_endpoint_returns_keys(settings: Settings) -> None:
    app = build_app(settings)
    with TestClient(app) as client:
        resp = client.get("/oauth/jwks.json")

    assert resp.status_code == 200
    body = resp.json()
    assert "keys" in body
    assert len(body["keys"]) >= 1
    assert body["keys"][0]["kty"] == "RSA"
    assert body["keys"][0]["use"] == "sig"
    assert body["keys"][0]["alg"] == "RS256"
    assert "kid" in body["keys"][0]


def test_register_endpoint_accepts_allowlisted_redirect(settings: Settings) -> None:
    app = build_app(settings)
    with TestClient(app) as client:
        resp = client.post(
            "/oauth/register",
            json={
                "client_name": "Test Client",
                "redirect_uris": ["https://claude.ai/api/mcp/auth_callback"],
                "token_endpoint_auth_method": "client_secret_post",
            },
        )

    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["client_id"]
    assert body["client_secret"]
    assert body["redirect_uris"] == ["https://claude.ai/api/mcp/auth_callback"]


def test_register_endpoint_rejects_off_allowlist_redirect(settings: Settings) -> None:
    app = build_app(settings)
    with TestClient(app) as client:
        resp = client.post(
            "/oauth/register",
            json={
                "client_name": "Bad Client",
                "redirect_uris": ["https://evil.example.com/cb"],
            },
        )

    assert resp.status_code == 400
    assert resp.json()["error"] == "invalid_redirect_uri"


def test_register_endpoint_accepts_vscode_redirect_set(settings: Settings) -> None:
    """VS Code 1.108+ submits four redirect_uris; all four shapes are allowlisted."""
    app = build_app(settings)
    vscode_uris = [
        "https://vscode.dev/redirect",
        "https://insiders.vscode.dev/redirect",
        "http://127.0.0.1:33418/callback",
        "http://localhost:33418/callback",
    ]
    with TestClient(app) as client:
        resp = client.post(
            "/oauth/register",
            json={
                "client_name": "Visual Studio Code",
                "redirect_uris": vscode_uris,
                "token_endpoint_auth_method": "none",
            },
        )

    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["client_id"]
    # All four matched the allowlist, so all four are stored.
    assert body["redirect_uris"] == vscode_uris


def test_register_endpoint_filters_not_rejects_mixed_set(settings: Settings) -> None:
    """Filter-don't-reject: drop off-allowlist URIs, accept if >=1 matches."""
    app = build_app(settings)
    with TestClient(app) as client:
        resp = client.post(
            "/oauth/register",
            json={
                "client_name": "Mixed Client",
                "redirect_uris": [
                    "https://claude.ai/api/mcp/auth_callback",
                    "https://evil.example.com/cb",
                ],
                "token_endpoint_auth_method": "none",
            },
        )

    assert resp.status_code == 201, resp.text
    # Only the allowlisted URI is stored; the disallowed one is dropped.
    assert resp.json()["redirect_uris"] == ["https://claude.ai/api/mcp/auth_callback"]


def test_register_endpoint_rejects_loopback_subdomain_bypass(settings: Settings) -> None:
    """`http://127.0.0.1.evil.com/` must NOT match the `http://127.0.0.1/` prefix."""
    app = build_app(settings)
    with TestClient(app) as client:
        resp = client.post(
            "/oauth/register",
            json={
                "client_name": "Evil Client",
                "redirect_uris": ["http://127.0.0.1.evil.com/cb"],
            },
        )

    assert resp.status_code == 400
    assert resp.json()["error"] == "invalid_redirect_uri"


def test_path_suffixed_prm_returns_mcp_endpoint_as_resource(settings: Settings) -> None:
    """RFC 9728 §3.3: path-suffixed PRM `resource` must equal the MCP endpoint URL."""
    app = build_app(settings)
    with TestClient(app) as client:
        resp = client.get("/.well-known/oauth-protected-resource/mcp")

    assert resp.status_code == 200, resp.text
    body = resp.json()
    # VS Code requires the exact endpoint URL, not the origin. The contract
    # (pocketid-mcp-as v1.1) makes the MCP resource path app-declared; this
    # server keeps /mcp.
    assert body["resource"] == "https://mcp.example.com/mcp"
    assert body["authorization_servers"] == ["https://mcp.example.com"]


def test_origin_prm_still_uses_origin_as_resource(settings: Settings) -> None:
    """Origin-root PRM keeps `resource`=origin for claude.ai compatibility."""
    app = build_app(settings)
    with TestClient(app) as client:
        resp = client.get("/.well-known/oauth-protected-resource")

    assert resp.status_code == 200
    assert resp.json()["resource"] == "https://mcp.example.com"


def test_401_emits_resource_metadata_in_www_authenticate(settings: Settings) -> None:
    """RFC 9728 §5.3: the 401 WWW-Authenticate points at the path-suffixed PRM."""
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
    www_auth = resp.headers["www-authenticate"]
    assert 'Bearer realm="homelab-mcp"' in www_auth
    assert (
        'resource_metadata="https://mcp.example.com/.well-known/oauth-protected-resource/mcp"'
        in www_auth
    )


def test_mcp_endpoint_still_requires_auth(settings: Settings) -> None:
    """The discovery allowlist must not leak through to /mcp."""
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


def test_arbitrary_well_known_path_not_allowlisted(settings: Settings) -> None:
    """Allowlist is exact-match; siblings under /.well-known/ should still be challenged."""
    app = build_app(settings)
    with TestClient(app) as client:
        resp = client.get("/.well-known/openid-configuration")

    assert resp.status_code == 401
