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


def _set_required(monkeypatch: pytest.MonkeyPatch) -> None:
    """Populate the minimum env vars required when oauth_required=True."""
    monkeypatch.setenv("HOMELAB_MCP_PUBLIC_BASE_URL", "https://mcp.example.com")
    monkeypatch.setenv("HOMELAB_MCP_POCKETID_ISSUER", "https://id.example.com")
    monkeypatch.setenv("HOMELAB_MCP_POCKETID_CLIENT_ID", "mcp-client")
    monkeypatch.setenv("HOMELAB_MCP_POCKETID_CLIENT_SECRET", "shh")


def test_required_oauth_fails_without_config(monkeypatch: pytest.MonkeyPatch) -> None:
    # OAuth required is the default; missing required vars must raise.
    with pytest.raises(ValueError, match="POCKETID_ISSUER"):
        Settings()


def test_oauth_disabled_works_with_no_config(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HOMELAB_MCP_OAUTH_REQUIRED", "false")
    s = Settings()
    assert s.oauth_required is False
    assert s.pocketid_issuer == ""


def test_required_oauth_passes_when_set(monkeypatch: pytest.MonkeyPatch) -> None:
    _set_required(monkeypatch)
    s = Settings()
    assert s.public_base_url == "https://mcp.example.com"
    assert s.pocketid_issuer == "https://id.example.com"
    assert s.pocketid_client_id == "mcp-client"
    # Derived values.
    assert s.issuer == "https://mcp.example.com"
    assert s.resource_url == "https://mcp.example.com"
    assert s.pocketid_redirect_uri == "https://mcp.example.com/oauth/callback"
    # Contract (pocketid-mcp-as v1.1) makes the MCP resource path app-declared;
    # this server keeps /mcp.
    assert s.mcp_path == "/mcp"
    assert s.mcp_resource_url == "https://mcp.example.com/mcp"
    assert s.prm_path_suffixed == "/.well-known/oauth-protected-resource/mcp"


def test_public_base_url_trailing_slash_normalized(monkeypatch: pytest.MonkeyPatch) -> None:
    _set_required(monkeypatch)
    monkeypatch.setenv("HOMELAB_MCP_PUBLIC_BASE_URL", "https://mcp.example.com/")
    s = Settings()
    assert s.issuer == "https://mcp.example.com"
    assert s.pocketid_redirect_uri == "https://mcp.example.com/oauth/callback"


def test_required_oauth_fails_when_public_base_url_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("HOMELAB_MCP_POCKETID_ISSUER", "https://id.example.com")
    monkeypatch.setenv("HOMELAB_MCP_POCKETID_CLIENT_ID", "mcp-client")
    monkeypatch.setenv("HOMELAB_MCP_POCKETID_CLIENT_SECRET", "shh")
    with pytest.raises(ValueError, match="PUBLIC_BASE_URL"):
        Settings()


def test_defaults(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HOMELAB_MCP_OAUTH_REQUIRED", "false")
    s = Settings()
    assert s.bind_address == "127.0.0.1"
    assert s.port == 9200
    assert s.cooklang_base_url == "https://cook.holthome.net"
    assert s.federation_base_url == "https://fedcook.holthome.net"
    assert s.gatus_base_url == "https://gatus.holthome.net"
    # OAuth defaults.
    assert s.oauth_access_token_lifetime_seconds == 86400
    assert s.oauth_code_lifetime_seconds == 120
    assert s.oauth_redirect_uri_allowlist == [
        "https://claude.ai/",
        "https://claude.com/",
        "https://vscode.dev/redirect",
        "https://insiders.vscode.dev/redirect",
        "http://127.0.0.1:",
        "http://127.0.0.1/",
        "http://localhost:",
        "http://localhost/",
    ]


def test_redirect_uri_allowlist_overridable(monkeypatch: pytest.MonkeyPatch) -> None:
    _set_required(monkeypatch)
    monkeypatch.setenv(
        "HOMELAB_MCP_OAUTH_REDIRECT_URI_ALLOWLIST",
        '["https://example.com/cb"]',
    )
    s = Settings()
    assert s.oauth_redirect_uri_allowlist == ["https://example.com/cb"]
