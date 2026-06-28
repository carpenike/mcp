"""Tests for the public contract-hosting routes (Part B).

mcp.holthome.net is the designated public home of the pocketid-mcp-as
contract. These routes must be unauthenticated, GET-only, CORS-open, and
serve the contract content byte-for-byte. The content is NOT committed: it's
fetched from upstream@pinned-ref at build time (hatch_build.py) into
contract/, which is what the dev/editable runtime serves.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from starlette.testclient import TestClient

from homelab_mcp import contract as contract_hosting
from homelab_mcp.app import build_app
from homelab_mcp.config import Settings

STAGED_DIR = Path(__file__).resolve().parent.parent / "contract"

# The contract content is fetched-at-build (gitignored). If a fresh checkout
# hasn't staged it yet, skip rather than hard-fail with a confusing error.
requires_staged_contract = pytest.mark.skipif(
    not (STAGED_DIR / "contract.json").is_file(),
    reason="contract content not staged; run `make contract-pull` (or reinstall)",
)

pytestmark = requires_staged_contract


@pytest.fixture
def settings(monkeypatch: pytest.MonkeyPatch, tmp_path) -> Settings:
    monkeypatch.setenv("HOMELAB_MCP_PUBLIC_BASE_URL", "https://mcp.example.com")
    monkeypatch.setenv("HOMELAB_MCP_POCKETID_ISSUER", "https://id.example.com")
    monkeypatch.setenv("HOMELAB_MCP_POCKETID_CLIENT_ID", "mcp-client")
    monkeypatch.setenv("HOMELAB_MCP_POCKETID_CLIENT_SECRET", "shh")
    monkeypatch.setenv("HOMELAB_MCP_OAUTH_SIGNING_KEY_PATH", str(tmp_path / "signing-key.pem"))
    monkeypatch.setenv("HOMELAB_MCP_OAUTH_STATE_DB_PATH", str(tmp_path / "state.db"))
    return Settings()


def test_contract_json_served_unauthenticated_with_correct_headers(settings: Settings) -> None:
    app = build_app(settings)
    with TestClient(app) as client:
        resp = client.get("/.well-known/mcp-as-contract.json")

    assert resp.status_code == 200
    assert resp.headers["access-control-allow-origin"] == "*"
    assert resp.headers["cache-control"] == "public, max-age=300"
    assert resp.headers["content-type"] == "application/json"
    assert resp.headers["x-contract-version"] == "1.1.0"


def test_contract_json_byte_matches_staged_copy(settings: Settings) -> None:
    """Served bytes must equal the staged contract.json (fetched from upstream@ref)."""
    staged_bytes = (STAGED_DIR / "contract.json").read_bytes()
    app = build_app(settings)
    with TestClient(app) as client:
        resp = client.get("/.well-known/mcp-as-contract.json")

    assert resp.content == staged_bytes
    # And the parsed JSON is order-insensitively identical.
    assert resp.json() == json.loads(staged_bytes)


def test_contract_markdown_served_as_markdown(settings: Settings) -> None:
    app = build_app(settings)
    with TestClient(app) as client:
        resp = client.get("/contract")

    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("text/markdown")
    assert resp.headers["access-control-allow-origin"] == "*"
    assert resp.headers["x-contract-version"] == "1.1.0"
    assert resp.text == (STAGED_DIR / "CONTRACT.md").read_text(encoding="utf-8")


def test_contract_routes_served_even_when_oauth_disabled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Public contract docs stay outside the OAuth path — present in dev mode too."""
    monkeypatch.setenv("HOMELAB_MCP_OAUTH_REQUIRED", "false")
    app = build_app(Settings())
    with TestClient(app) as client:
        assert client.get("/.well-known/mcp-as-contract.json").status_code == 200
        assert client.get("/contract").status_code == 200


def test_contract_preflight_returns_cors(settings: Settings) -> None:
    app = build_app(settings)
    with TestClient(app) as client:
        resp = client.options("/.well-known/mcp-as-contract.json")

    assert resp.status_code == 204
    assert resp.headers["access-control-allow-origin"] == "*"


def test_contract_paths_are_in_middleware_allowlist() -> None:
    assert "/.well-known/mcp-as-contract.json" in contract_hosting.CONTRACT_PATHS
    assert "/contract" in contract_hosting.CONTRACT_PATHS
