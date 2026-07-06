"""Security-hardening regression tests for the OAuth AS.

Covers the fixes for the frontend-tool-review findings:

  - F1  strict redirect-URI matching (userinfo bypass rejected)
  - F2  refresh-token reuse detection + rotation-family revocation
  - F4  Cache-Control: no-store on token / DCR / error responses
  - F5  DCR metadata size caps
"""

from __future__ import annotations

import base64
import hashlib
import secrets
import time

import pytest
from pytest_httpx import HTTPXMock
from starlette.testclient import TestClient

from homelab_mcp.app import build_app
from homelab_mcp.config import Settings
from homelab_mcp.oauth_provider import _redirect_allowed
from homelab_mcp.oauth_state import IssuedRefreshToken, OAuthState


def _b64url_no_pad(b: bytes) -> str:
    return base64.urlsafe_b64encode(b).rstrip(b"=").decode("ascii")


@pytest.fixture
def settings(monkeypatch: pytest.MonkeyPatch, tmp_path) -> Settings:
    monkeypatch.setenv("HOMELAB_MCP_PUBLIC_BASE_URL", "https://mcp.example.com")
    monkeypatch.setenv("HOMELAB_MCP_POCKETID_ISSUER", "https://id.example.com")
    monkeypatch.setenv("HOMELAB_MCP_POCKETID_CLIENT_ID", "mcp-client")
    monkeypatch.setenv("HOMELAB_MCP_POCKETID_CLIENT_SECRET", "shh")
    monkeypatch.setenv("HOMELAB_MCP_OAUTH_SIGNING_KEY_PATH", str(tmp_path / "signing-key.pem"))
    monkeypatch.setenv("HOMELAB_MCP_OAUTH_STATE_DB_PATH", str(tmp_path / "state.db"))
    return Settings()


# ── F1: redirect-URI matching ────────────────────────────────────────


@pytest.mark.parametrize(
    "uri",
    [
        "https://claude.ai/api/mcp/auth_callback",
        "https://claude.com/cb",
        "http://127.0.0.1:1234/callback",
        "http://localhost:8765/cb",
        "http://localhost/cb",
        "https://vscode.dev/redirect",
    ],
)
def test_redirect_allowed_accepts_legitimate(uri: str, settings: Settings) -> None:
    assert _redirect_allowed(uri, settings) is True


@pytest.mark.parametrize(
    "uri",
    [
        # The userinfo bypass: real host is evil.com, but a naive
        # startswith("http://localhost:") / ("http://127.0.0.1:") passes.
        "http://localhost:1234@evil.com/cb",
        "http://127.0.0.1:9@evil.com/cb",
        "http://localhost:1@evil.com:8080/cb",
        # Plain off-allowlist and malformed.
        "https://evil.com/cb",
        "http://localhostx/cb",
        "ftp://localhost/cb",
        "not-a-url",
    ],
)
def test_redirect_allowed_rejects_bypass_and_offlist(uri: str, settings: Settings) -> None:
    assert _redirect_allowed(uri, settings) is False


def test_dcr_drops_userinfo_bypass_uri(settings: Settings, httpx_mock: HTTPXMock) -> None:
    """DCR must filter out a userinfo-bypass redirect while keeping a valid one."""
    app = build_app(settings)
    with TestClient(app, follow_redirects=False) as client:
        resp = client.post(
            "/oauth/register",
            json={
                "client_name": "Claude",
                "redirect_uris": [
                    "https://claude.ai/cb",
                    "http://localhost:1234@evil.com/cb",
                ],
            },
        )
        assert resp.status_code == 201
        assert resp.json()["redirect_uris"] == ["https://claude.ai/cb"]


def test_dcr_all_invalid_redirects_400(settings: Settings) -> None:
    app = build_app(settings)
    with TestClient(app, follow_redirects=False) as client:
        resp = client.post(
            "/oauth/register",
            json={"redirect_uris": ["http://localhost:1@evil.com/cb"]},
        )
        assert resp.status_code == 400
        assert resp.json()["error"] == "invalid_redirect_uri"


def test_authorize_rejects_unstored_redirect(settings: Settings, httpx_mock: HTTPXMock) -> None:
    """/authorize must reject a redirect_uri that was never stored for the client."""
    app = build_app(settings)
    with TestClient(app, follow_redirects=False) as client:
        reg = client.post(
            "/oauth/register",
            json={"client_name": "Claude", "redirect_uris": ["https://claude.ai/cb"]},
        ).json()
        cv = secrets.token_urlsafe(64)
        cc = _b64url_no_pad(hashlib.sha256(cv.encode()).digest())
        resp = client.get(
            "/oauth/authorize",
            params={
                "response_type": "code",
                "client_id": reg["client_id"],
                "redirect_uri": "https://evil.com/cb",
                "code_challenge": cc,
                "code_challenge_method": "S256",
            },
        )
        assert resp.status_code == 400
        assert resp.json()["error"] == "invalid_redirect_uri"


# ── F5: DCR metadata caps ────────────────────────────────────────────


def test_dcr_rejects_too_many_redirect_uris(settings: Settings) -> None:
    app = build_app(settings)
    with TestClient(app, follow_redirects=False) as client:
        resp = client.post(
            "/oauth/register",
            json={"redirect_uris": [f"https://claude.ai/cb{i}" for i in range(20)]},
        )
        assert resp.status_code == 400
        assert resp.json()["error"] == "invalid_redirect_uri"


def test_dcr_rejects_oversized_redirect_uri(settings: Settings) -> None:
    app = build_app(settings)
    with TestClient(app, follow_redirects=False) as client:
        resp = client.post(
            "/oauth/register",
            json={"redirect_uris": ["https://claude.ai/" + "a" * 600]},
        )
        assert resp.status_code == 400


def test_dcr_rejects_oversized_client_name(settings: Settings) -> None:
    app = build_app(settings)
    with TestClient(app, follow_redirects=False) as client:
        resp = client.post(
            "/oauth/register",
            json={
                "client_name": "x" * 300,
                "redirect_uris": ["https://claude.ai/cb"],
            },
        )
        assert resp.status_code == 400
        assert resp.json()["error"] == "invalid_client_metadata"


# ── F4: no-store headers ─────────────────────────────────────────────


def test_dcr_and_error_responses_are_no_store(settings: Settings) -> None:
    app = build_app(settings)
    with TestClient(app, follow_redirects=False) as client:
        ok = client.post(
            "/oauth/register",
            json={"redirect_uris": ["https://claude.ai/cb"]},
        )
        assert ok.headers.get("cache-control") == "no-store"
        err = client.post("/oauth/register", json={"redirect_uris": []})
        assert err.status_code == 400
        assert err.headers.get("cache-control") == "no-store"


# ── F2: refresh reuse detection (unit-level, both backends) ──────────


@pytest.mark.parametrize("db", [None, "file"])
async def test_refresh_reuse_revokes_family(tmp_path, db: str | None) -> None:
    """Replaying a rotated refresh token revokes the whole rotation family."""
    db_path = None if db is None else str(tmp_path / "reuse.db")
    state = OAuthState.open(db_path, consumed_retention_seconds=3600)

    fam = "family-abc"
    expires = time.time() + 3600
    r1 = secrets.token_urlsafe(48)
    await state.store_refresh(
        r1, IssuedRefreshToken(client_id="c", user_email="u@x", expires_at=expires, family_id=fam)
    )

    # Rotate r1 -> r2 (same family).
    consumed = await state.consume_refresh(r1)
    assert consumed is not None
    assert consumed.family_id == fam
    r2 = secrets.token_urlsafe(48)
    await state.store_refresh(
        r2, IssuedRefreshToken(client_id="c", user_email="u@x", expires_at=expires, family_id=fam)
    )

    # r2 is currently valid.
    # Replay the already-consumed r1 -> reuse detected -> family revoked.
    assert await state.consume_refresh(r1) is None

    # r2 (the live descendant) is now revoked too.
    assert await state.consume_refresh(r2) is None


async def test_refresh_unknown_token_is_not_reuse(tmp_path) -> None:
    """A genuinely-unknown token returns None without touching other families."""
    state = OAuthState.open(str(tmp_path / "u.db"), consumed_retention_seconds=3600)
    expires = time.time() + 3600
    good = secrets.token_urlsafe(48)
    await state.store_refresh(
        good,
        IssuedRefreshToken(client_id="c", user_email="u@x", expires_at=expires, family_id="fam"),
    )
    assert await state.consume_refresh("never-issued") is None
    # The unrelated live token still works.
    assert await state.consume_refresh(good) is not None
