"""End-to-end OAuth provider flow test.

Mocks PocketID via pytest-httpx and exercises:

  1. /oauth/register (DCR)
  2. /oauth/authorize (Claude → us → 302 to PocketID)
  3. /oauth/callback (PocketID → us; we exchange + mint our code)
  4. /oauth/token (Claude → us; we mint a JWT)
  5. /mcp with the issued JWT (JWT middleware accepts it)
"""

from __future__ import annotations

import base64
import hashlib
import json
import secrets
import time
from typing import Any
from urllib.parse import parse_qs, urlparse

import jwt
import pytest
from authlib.jose import JsonWebKey, JsonWebToken
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from pytest_httpx import HTTPXMock
from starlette.testclient import TestClient

from homelab_mcp.app import build_app
from homelab_mcp.config import Settings


def _b64url_no_pad(b: bytes) -> str:
    return base64.urlsafe_b64encode(b).rstrip(b"=").decode("ascii")


@pytest.fixture
def upstream_key() -> rsa.RSAPrivateKey:
    """RSA keypair representing PocketID's signing key for ID tokens."""
    return rsa.generate_private_key(public_exponent=65537, key_size=2048)


@pytest.fixture
def upstream_jwks(upstream_key: rsa.RSAPrivateKey) -> dict[str, Any]:
    pub_pem = upstream_key.public_key().public_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PublicFormat.SubjectPublicKeyInfo,
    )
    jwk = JsonWebKey.import_key(pub_pem, {"kty": "RSA", "use": "sig", "alg": "RS256"})
    d = dict(jwk.as_dict())
    d["kid"] = "pocketid-test-kid"
    return {"keys": [d]}


@pytest.fixture
def settings(monkeypatch: pytest.MonkeyPatch, tmp_path) -> Settings:
    monkeypatch.setenv("HOMELAB_MCP_PUBLIC_BASE_URL", "https://mcp.example.com")
    monkeypatch.setenv("HOMELAB_MCP_POCKETID_ISSUER", "https://id.example.com")
    monkeypatch.setenv("HOMELAB_MCP_POCKETID_CLIENT_ID", "mcp-client")
    monkeypatch.setenv("HOMELAB_MCP_POCKETID_CLIENT_SECRET", "shh")
    monkeypatch.setenv("HOMELAB_MCP_OAUTH_SIGNING_KEY_PATH", str(tmp_path / "signing-key.pem"))
    return Settings()


@pytest.fixture(autouse=True)
def _reset_provider_cache():
    """oauth_provider caches PocketID metadata in module scope; reset per test."""
    from homelab_mcp import oauth_provider

    oauth_provider._PROVIDER_METADATA_CACHE = {}
    oauth_provider._PROVIDER_METADATA_EXPIRY = 0.0
    yield
    oauth_provider._PROVIDER_METADATA_CACHE = {}
    oauth_provider._PROVIDER_METADATA_EXPIRY = 0.0


def _mock_pocketid_discovery(httpx_mock: HTTPXMock) -> None:
    httpx_mock.add_response(
        url="https://id.example.com/.well-known/openid-configuration",
        json={
            "issuer": "https://id.example.com",
            "authorization_endpoint": "https://id.example.com/authorize",
            "token_endpoint": "https://id.example.com/token",
            "jwks_uri": "https://id.example.com/jwks",
        },
    )


def _mint_upstream_id_token(
    upstream_key: rsa.RSAPrivateKey,
    *,
    aud: str,
    nonce: str,
    email: str = "user@example.com",
) -> str:
    now = int(time.time())
    pem = upstream_key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    )
    token = JsonWebToken(["RS256"]).encode(
        header={"alg": "RS256", "kid": "pocketid-test-kid", "typ": "JWT"},
        payload={
            "iss": "https://id.example.com",
            "aud": aud,
            "sub": "user-123",
            "email": email,
            "nonce": nonce,
            "iat": now,
            "nbf": now,
            "exp": now + 300,
        },
        key=pem,
    )
    return token.decode("ascii")


async def test_full_oauth_flow(
    settings: Settings,
    upstream_key: rsa.RSAPrivateKey,
    upstream_jwks: dict[str, Any],
    httpx_mock: HTTPXMock,
) -> None:
    """End-to-end: register → authorize → callback → token → /mcp with JWT."""
    # NOTE: use TestClient as a context manager so FastMCP's lifespan
    # (which creates the StreamableHTTPSessionManager task group) runs.
    # Without this the final /mcp leg crashes with "Task group not
    # initialized".
    app = build_app(settings)
    _mock_pocketid_discovery(httpx_mock)
    with TestClient(app, follow_redirects=False) as client:
        await _run_full_flow(client, upstream_key, upstream_jwks, httpx_mock)


async def _run_full_flow(
    client: TestClient,
    upstream_key: rsa.RSAPrivateKey,
    upstream_jwks: dict[str, Any],
    httpx_mock: HTTPXMock,
) -> None:
    # ── 1. DCR ───────────────────────────────────────────────────────
    reg_resp = client.post(
        "/oauth/register",
        json={
            "client_name": "Claude",
            "redirect_uris": ["https://claude.ai/api/mcp/auth_callback"],
            "token_endpoint_auth_method": "client_secret_post",
        },
    )
    assert reg_resp.status_code == 201
    reg = reg_resp.json()
    client_id, client_secret = reg["client_id"], reg["client_secret"]

    # ── 2. /authorize ────────────────────────────────────────────────
    code_verifier = secrets.token_urlsafe(64)
    code_challenge = _b64url_no_pad(hashlib.sha256(code_verifier.encode("ascii")).digest())

    authz_resp = client.get(
        "/oauth/authorize",
        params={
            "response_type": "code",
            "client_id": client_id,
            "redirect_uri": "https://claude.ai/api/mcp/auth_callback",
            "code_challenge": code_challenge,
            "code_challenge_method": "S256",
            "state": "claude-state-xyz",
            "scope": "mcp",
        },
    )
    assert authz_resp.status_code == 302
    location = authz_resp.headers["location"]
    assert location.startswith("https://id.example.com/authorize?")

    upstream_params = parse_qs(urlparse(location).query)
    state_token = upstream_params["state"][0]
    nonce = upstream_params["nonce"][0]
    assert upstream_params["client_id"] == ["mcp-client"]
    assert upstream_params["redirect_uri"] == ["https://mcp.example.com/oauth/callback"]
    assert upstream_params["scope"] == ["openid email profile"]
    # PocketID enforces PKCE; we must send a code_challenge upstream
    # and present the matching verifier on the upstream token exchange.
    assert upstream_params["code_challenge_method"] == ["S256"]
    assert "code_challenge" in upstream_params

    # ── 3. /oauth/callback ───────────────────────────────────────────
    # Stub PocketID's token + JWKS endpoints for the callback's S2S calls.
    id_token = _mint_upstream_id_token(
        upstream_key, aud="mcp-client", nonce=nonce, email="user@example.com"
    )
    httpx_mock.add_response(
        url="https://id.example.com/token",
        json={
            "access_token": "upstream-access",
            "id_token": id_token,
            "token_type": "Bearer",
            "expires_in": 3600,
        },
    )
    httpx_mock.add_response(url="https://id.example.com/jwks", json=upstream_jwks)

    cb_resp = client.get("/oauth/callback", params={"code": "upstream-code", "state": state_token})
    assert cb_resp.status_code == 302, cb_resp.text
    cb_location = cb_resp.headers["location"]
    assert cb_location.startswith("https://claude.ai/api/mcp/auth_callback?")
    cb_params = parse_qs(urlparse(cb_location).query)
    our_code = cb_params["code"][0]
    assert cb_params["state"] == ["claude-state-xyz"]

    # ── 4. /oauth/token ──────────────────────────────────────────────
    token_resp = client.post(
        "/oauth/token",
        data={
            "grant_type": "authorization_code",
            "code": our_code,
            "redirect_uri": "https://claude.ai/api/mcp/auth_callback",
            "code_verifier": code_verifier,
            "client_id": client_id,
            "client_secret": client_secret,
        },
    )
    assert token_resp.status_code == 200, token_resp.text
    tokens = token_resp.json()
    access_token = tokens["access_token"]
    assert tokens["token_type"] == "Bearer"
    assert tokens["expires_in"] == 86400

    # JWT shape — verify with pyjwt against our published JWKS.
    jwks_resp = client.get("/oauth/jwks.json")
    public_jwk = jwks_resp.json()["keys"][0]
    public_key = jwt.PyJWK(public_jwk).key
    decoded = jwt.decode(
        access_token,
        public_key,
        algorithms=["RS256"],
        issuer="https://mcp.example.com",
        audience="https://mcp.example.com",
    )
    assert decoded["sub"] == "user@example.com"
    assert decoded["email"] == "user@example.com"
    assert decoded["client_id"] == client_id

    # ── 5. /mcp with the JWT ─────────────────────────────────────────
    mcp_resp = client.post(
        "/mcp",
        headers={
            "Authorization": f"Bearer {access_token}",
            "Accept": "application/json, text/event-stream",
            "Content-Type": "application/json",
        },
        json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
    )
    # We're hitting a real MCP transport here — it requires session
    # negotiation (mcp-session-id) before tools/list, so a 400 from the
    # MCP layer is fine. What we want to confirm is that the AUTH layer
    # didn't reject the token. Anything other than 401 is success.
    assert mcp_resp.status_code != 401, f"JWT was rejected by auth middleware: {mcp_resp.text}"


async def test_code_is_single_use(
    settings: Settings,
    upstream_key: rsa.RSAPrivateKey,
    upstream_jwks: dict[str, Any],
    httpx_mock: HTTPXMock,
) -> None:
    """Same authorization_code can't be exchanged twice."""
    app = build_app(settings)
    client = TestClient(app, follow_redirects=False)
    _mock_pocketid_discovery(httpx_mock)

    reg = client.post(
        "/oauth/register",
        json={
            "client_name": "Claude",
            "redirect_uris": ["https://claude.ai/api/mcp/auth_callback"],
        },
    ).json()
    client_id, client_secret = reg["client_id"], reg["client_secret"]

    code_verifier = secrets.token_urlsafe(64)
    code_challenge = _b64url_no_pad(hashlib.sha256(code_verifier.encode("ascii")).digest())

    authz = client.get(
        "/oauth/authorize",
        params={
            "response_type": "code",
            "client_id": client_id,
            "redirect_uri": "https://claude.ai/api/mcp/auth_callback",
            "code_challenge": code_challenge,
            "code_challenge_method": "S256",
        },
    )
    state_token = parse_qs(urlparse(authz.headers["location"]).query)["state"][0]
    nonce = parse_qs(urlparse(authz.headers["location"]).query)["nonce"][0]

    id_token = _mint_upstream_id_token(upstream_key, aud="mcp-client", nonce=nonce)
    httpx_mock.add_response(
        url="https://id.example.com/token",
        json={"id_token": id_token, "access_token": "x", "token_type": "Bearer"},
    )
    httpx_mock.add_response(url="https://id.example.com/jwks", json=upstream_jwks)

    cb = client.get("/oauth/callback", params={"code": "upstream", "state": state_token})
    our_code = parse_qs(urlparse(cb.headers["location"]).query)["code"][0]

    common = {
        "grant_type": "authorization_code",
        "code": our_code,
        "redirect_uri": "https://claude.ai/api/mcp/auth_callback",
        "code_verifier": code_verifier,
        "client_id": client_id,
        "client_secret": client_secret,
    }
    first = client.post("/oauth/token", data=common)
    second = client.post("/oauth/token", data=common)
    assert first.status_code == 200
    assert second.status_code == 400
    assert second.json()["error"] == "invalid_grant"


async def test_bad_pkce_rejected(
    settings: Settings,
    upstream_key: rsa.RSAPrivateKey,
    upstream_jwks: dict[str, Any],
    httpx_mock: HTTPXMock,
) -> None:
    """A wrong code_verifier fails PKCE check."""
    app = build_app(settings)
    client = TestClient(app, follow_redirects=False)
    _mock_pocketid_discovery(httpx_mock)

    reg = client.post(
        "/oauth/register",
        json={
            "client_name": "Claude",
            "redirect_uris": ["https://claude.ai/api/mcp/auth_callback"],
        },
    ).json()
    client_id, client_secret = reg["client_id"], reg["client_secret"]

    code_verifier = secrets.token_urlsafe(64)
    code_challenge = _b64url_no_pad(hashlib.sha256(code_verifier.encode("ascii")).digest())

    authz = client.get(
        "/oauth/authorize",
        params={
            "response_type": "code",
            "client_id": client_id,
            "redirect_uri": "https://claude.ai/api/mcp/auth_callback",
            "code_challenge": code_challenge,
            "code_challenge_method": "S256",
        },
    )
    state_token = parse_qs(urlparse(authz.headers["location"]).query)["state"][0]
    nonce = parse_qs(urlparse(authz.headers["location"]).query)["nonce"][0]

    id_token = _mint_upstream_id_token(upstream_key, aud="mcp-client", nonce=nonce)
    httpx_mock.add_response(
        url="https://id.example.com/token",
        json={"id_token": id_token, "access_token": "x", "token_type": "Bearer"},
    )
    httpx_mock.add_response(url="https://id.example.com/jwks", json=upstream_jwks)

    cb = client.get("/oauth/callback", params={"code": "upstream", "state": state_token})
    our_code = parse_qs(urlparse(cb.headers["location"]).query)["code"][0]

    resp = client.post(
        "/oauth/token",
        data={
            "grant_type": "authorization_code",
            "code": our_code,
            "redirect_uri": "https://claude.ai/api/mcp/auth_callback",
            "code_verifier": "wrong-verifier",
            "client_id": client_id,
            "client_secret": client_secret,
        },
    )
    assert resp.status_code == 400
    assert resp.json()["error"] == "invalid_grant"


async def test_user_allowlist_blocks_unknown_email(
    monkeypatch: pytest.MonkeyPatch,
    settings: Settings,
    upstream_key: rsa.RSAPrivateKey,
    upstream_jwks: dict[str, Any],
    httpx_mock: HTTPXMock,
) -> None:
    """If oauth_user_allowlist is set, an off-list email is blocked at callback."""
    monkeypatch.setenv("HOMELAB_MCP_OAUTH_USER_ALLOWLIST", json.dumps(["allowed@example.com"]))
    settings = Settings()
    app = build_app(settings)
    client = TestClient(app, follow_redirects=False)
    _mock_pocketid_discovery(httpx_mock)

    reg = client.post(
        "/oauth/register",
        json={
            "client_name": "Claude",
            "redirect_uris": ["https://claude.ai/api/mcp/auth_callback"],
        },
    ).json()
    client_id = reg["client_id"]

    code_verifier = secrets.token_urlsafe(64)
    code_challenge = _b64url_no_pad(hashlib.sha256(code_verifier.encode("ascii")).digest())
    authz = client.get(
        "/oauth/authorize",
        params={
            "response_type": "code",
            "client_id": client_id,
            "redirect_uri": "https://claude.ai/api/mcp/auth_callback",
            "code_challenge": code_challenge,
            "code_challenge_method": "S256",
        },
    )
    state_token = parse_qs(urlparse(authz.headers["location"]).query)["state"][0]
    nonce = parse_qs(urlparse(authz.headers["location"]).query)["nonce"][0]

    id_token = _mint_upstream_id_token(
        upstream_key, aud="mcp-client", nonce=nonce, email="evil@example.com"
    )
    httpx_mock.add_response(
        url="https://id.example.com/token",
        json={"id_token": id_token, "access_token": "x", "token_type": "Bearer"},
    )
    httpx_mock.add_response(url="https://id.example.com/jwks", json=upstream_jwks)

    cb = client.get("/oauth/callback", params={"code": "upstream", "state": state_token})
    assert cb.status_code == 403
    assert cb.json()["error"] == "access_denied"
