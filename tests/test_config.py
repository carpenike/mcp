"""Settings sanity tests — make sure the env-var contract holds."""

from __future__ import annotations

import os

import pytest

from homelab_mcp.config import Settings


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Strip any HOMELAB_MCP_* env vars to keep tests hermetic."""
    for key in list(os.environ):
        if key.startswith("HOMELAB_MCP_"):
            monkeypatch.delenv(key, raising=False)


def test_required_cf_access_fails_without_team_and_app_id(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("HOMELAB_MCP_CF_ACCESS_REQUIRED", "true")
    with pytest.raises(ValueError, match="CF_ACCESS"):
        Settings()


def test_optional_cf_access_ok_without_team(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HOMELAB_MCP_CF_ACCESS_REQUIRED", "false")
    s = Settings()
    assert s.cf_access_required is False
    assert s.cf_access_team == ""


def test_required_cf_access_passes_when_set(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HOMELAB_MCP_CF_ACCESS_TEAM", "bigheadltd")
    monkeypatch.setenv("HOMELAB_MCP_CF_ACCESS_APP_ID", "deadbeef" * 8)
    s = Settings()
    assert s.cf_access_team == "bigheadltd"
    assert s.cf_access_issuer == (
        f"https://bigheadltd.cloudflareaccess.com/cdn-cgi/access/sso/oidc/{'deadbeef' * 8}"
    )
    assert s.cf_access_jwks_url == s.cf_access_issuer + "/jwks"
    # Default audience == app_id
    assert s.cf_access_effective_audience == "deadbeef" * 8


def test_audience_override(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HOMELAB_MCP_CF_ACCESS_TEAM", "bigheadltd")
    monkeypatch.setenv("HOMELAB_MCP_CF_ACCESS_APP_ID", "deadbeef" * 8)
    monkeypatch.setenv("HOMELAB_MCP_CF_ACCESS_AUDIENCE", "custom-aud")
    s = Settings()
    assert s.cf_access_effective_audience == "custom-aud"


def test_defaults(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HOMELAB_MCP_CF_ACCESS_REQUIRED", "false")
    s = Settings()
    assert s.bind_address == "127.0.0.1"
    assert s.port == 9100
    assert s.cooklang_base_url == "https://cook.holthome.net"
    assert s.federation_base_url == "https://fedcook.holthome.net"
    assert s.gatus_base_url == "https://gatus.holthome.net"
