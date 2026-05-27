"""Tests for the RepLog tools and the tool-hop JWT mint helper (HOF-004)."""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock

import jwt as pyjwt
import pytest
from mcp.server.fastmcp import FastMCP

from homelab_mcp import oauth_provider, signing_key
from homelab_mcp.config import Settings
from homelab_mcp.tools import replog

# --- mint_tool_hop_token --------------------------------------------------


@pytest.fixture
def signed_settings(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Any
) -> tuple[Settings, signing_key.SigningKey]:
    """Return Settings + a freshly-generated SigningKey for token-mint tests."""
    monkeypatch.setenv("HOMELAB_MCP_PUBLIC_BASE_URL", "https://mcp.example.com")
    monkeypatch.setenv("HOMELAB_MCP_POCKETID_ISSUER", "https://id.example.com")
    monkeypatch.setenv("HOMELAB_MCP_POCKETID_CLIENT_ID", "mcp-client")
    monkeypatch.setenv("HOMELAB_MCP_POCKETID_CLIENT_SECRET", "shh")
    monkeypatch.setenv("HOMELAB_MCP_OAUTH_SIGNING_KEY_PATH", str(tmp_path / "key.pem"))
    s = Settings()
    return s, signing_key.load_or_create(s)


def test_mint_tool_hop_token_addresses_a_different_audience(
    signed_settings: tuple[Settings, signing_key.SigningKey],
) -> None:
    """The whole point of the helper: re-mint with `aud` set to a downstream resource."""
    s, key = signed_settings
    token = oauth_provider.mint_tool_hop_token(
        s,
        key,
        sub="ryan",
        email="ryan@example.com",
        audience="https://replog.example.com",
    )
    # Verify against the same key (skip JWKS dance — this is the public side).
    from cryptography.hazmat.primitives import serialization

    private = serialization.load_pem_private_key(key.private_pem, password=None)
    public = private.public_key()  # type: ignore[union-attr]
    claims = pyjwt.decode(
        token,
        public,
        algorithms=["RS256"],
        audience="https://replog.example.com",
        issuer=s.issuer,
    )
    assert claims["sub"] == "ryan"
    assert claims["email"] == "ryan@example.com"
    assert claims["aud"] == "https://replog.example.com"
    assert claims["iss"] == s.issuer  # different from aud — that's the whole point
    # Short-TTL default — caller may override.
    assert claims["exp"] - claims["iat"] == 60


def test_mint_tool_hop_token_respects_ttl_override(
    signed_settings: tuple[Settings, signing_key.SigningKey],
) -> None:
    s, key = signed_settings
    token = oauth_provider.mint_tool_hop_token(
        s, key, sub="x", email="x@x", audience="https://r.example.com", ttl_seconds=300
    )
    decoded = pyjwt.decode(token, options={"verify_signature": False})
    assert decoded["exp"] - decoded["iat"] == 300


def test_mint_tool_hop_token_aud_does_not_match_own_resource_url(
    signed_settings: tuple[Settings, signing_key.SigningKey],
) -> None:
    """Defense check: a tool-hop token addressed to RepLog must NOT validate against our OWN aud.

    This is the load-bearing replay defence: an attacker who intercepts
    a replog-addressed token cannot turn around and call our own /mcp
    endpoint with it.
    """
    s, key = signed_settings
    token = oauth_provider.mint_tool_hop_token(
        s, key, sub="x", email="x@x", audience="https://replog.example.com"
    )
    from cryptography.hazmat.primitives import serialization

    private = serialization.load_pem_private_key(key.private_pem, password=None)
    public = private.public_key()  # type: ignore[union-attr]
    with pytest.raises(pyjwt.InvalidAudienceError):
        pyjwt.decode(
            token,
            public,
            algorithms=["RS256"],
            audience=s.resource_url,  # OUR aud, not RepLog's
            issuer=s.issuer,
        )


# --- replog.register() guards -----------------------------------------------


def test_register_is_no_op_when_base_url_empty(monkeypatch: pytest.MonkeyPatch) -> None:
    """If HOMELAB_MCP_REPLOG_BASE_URL is empty, register() skips with a warning.

    This is how a deployment without RepLog disables the integration —
    no code changes, no env-var gymnastics, just leave it unset.
    """
    monkeypatch.setenv("HOMELAB_MCP_OAUTH_REQUIRED", "false")
    monkeypatch.delenv("HOMELAB_MCP_REPLOG_BASE_URL", raising=False)
    s = Settings()
    assert s.replog_base_url == ""

    mcp = FastMCP("test")

    def fake_mint(**kwargs: Any) -> str:
        return "fake-token"

    replog.register(mcp, s, fake_mint)
    import asyncio

    tools = asyncio.run(mcp.list_tools())
    replog_tools = [t for t in tools if t.name.startswith("replog_")]
    assert replog_tools == [], "expected no replog_* tools to be registered"


def test_register_mounts_all_v1_tools_when_configured(monkeypatch: pytest.MonkeyPatch) -> None:
    """With base_url set, every v1 tool from HOF-004's tool catalog must register.

    The exhaustive list is the doctrine — any addition or removal here
    is a deliberate scope change that should also update HOF-004.
    """
    monkeypatch.setenv("HOMELAB_MCP_OAUTH_REQUIRED", "false")
    monkeypatch.setenv("HOMELAB_MCP_REPLOG_BASE_URL", "http://127.0.0.1:5008")
    s = Settings()

    mcp = FastMCP("test")
    replog.register(mcp, s, lambda **_: "fake-token")

    import asyncio

    tools = asyncio.run(mcp.list_tools())
    got = sorted(t.name for t in tools if t.name.startswith("replog_"))

    # Group A reads (10) + Group B clerical writes (7) + Group C
    # generation (2). 19 total. Any expansion needs a HOF revision.
    want = sorted(
        [
            # Group A
            "replog_get_dashboard",
            "replog_get_athlete",
            "replog_list_workouts",
            "replog_get_workout",
            "replog_get_prescription",
            "replog_get_training_maxes",
            "replog_get_tm_history",
            "replog_list_journal",
            "replog_list_athlete_programs",
            "replog_list_athlete_equipment",
            # Group B
            "replog_create_workout",
            "replog_log_set",
            "replog_update_set",
            "replog_delete_set",
            "replog_update_workout_notes",
            "replog_log_body_weight",
            "replog_add_athlete_note",
            # Group C — enqueue + status ONLY (no execute).
            "replog_enqueue_program_generation",
            "replog_get_generation_status",
        ]
    )
    assert got == want, f"tool catalog drift\n got:  {got}\n want: {want}"


def test_no_execute_generation_tool_anywhere(monkeypatch: pytest.MonkeyPatch) -> None:
    """The doctrine line from HOF-004 [forbidden]: no MCP tool may commit a generation.

    Asserted by absence. If a future PR adds an `execute` tool, this
    test fails LOUDLY and the developer has to either justify the
    expansion (and update HOF-004 + the spec's [forbidden] block) or
    remove the tool.
    """
    monkeypatch.setenv("HOMELAB_MCP_OAUTH_REQUIRED", "false")
    monkeypatch.setenv("HOMELAB_MCP_REPLOG_BASE_URL", "http://127.0.0.1:5008")
    s = Settings()

    mcp = FastMCP("test")
    replog.register(mcp, s, lambda **_: "fake-token")

    import asyncio

    tools = asyncio.run(mcp.list_tools())
    forbidden = [
        t.name for t in tools if any(bad in t.name for bad in ("execute", "approve", "commit"))
    ]
    assert forbidden == [], (
        f"tool catalog drift: {forbidden} appear to commit / approve "
        "coaching decisions, which violates HOF-004 [forbidden]. "
        "Approval stays on the webui — see ADR 007 / 015."
    )


# --- registry signature-dispatch -------------------------------------------


def test_registry_skips_3arg_module_when_mint_token_none(monkeypatch: pytest.MonkeyPatch) -> None:
    """If a tool module declares register(mcp, settings, mint_token) but no
    minter is provided to register_all, the module is skipped with a
    warning rather than crashing the server. Matches the existing
    "missing register()" skip behavior.
    """
    monkeypatch.setenv("HOMELAB_MCP_OAUTH_REQUIRED", "false")
    monkeypatch.setenv("HOMELAB_MCP_REPLOG_BASE_URL", "http://127.0.0.1:5008")
    from homelab_mcp.tools._registry import register_all

    s = Settings()
    mcp = FastMCP("test")
    register_all(mcp, s, mint_token=None)

    import asyncio

    tools = asyncio.run(mcp.list_tools())
    # cooklang + gatus should still register (2-arg signature), replog skipped.
    names = {t.name for t in tools}
    assert any(n.startswith("cooklang_") for n in names), "cooklang should still register"
    assert not any(n.startswith("replog_") for n in names), (
        "replog should be skipped when mint_token is None"
    )


# --- tool-level: identity + outbound shape ---------------------------------


def _build_test_setup(monkeypatch: pytest.MonkeyPatch) -> tuple[FastMCP, list[dict[str, Any]]]:
    """Stand up a FastMCP with the replog tool module registered and a
    recording fake minter. Returns the mcp instance + a list captured
    of mint() invocations so tests can assert on what was minted.
    """
    monkeypatch.setenv("HOMELAB_MCP_OAUTH_REQUIRED", "false")
    monkeypatch.setenv("HOMELAB_MCP_REPLOG_BASE_URL", "http://127.0.0.1:5008")
    monkeypatch.setenv("HOMELAB_MCP_REPLOG_AUDIENCE", "https://replog.test")
    s = Settings()

    mints: list[dict[str, Any]] = []

    def recording_mint(**kwargs: Any) -> str:
        mints.append(kwargs)
        return f"FAKE.{kwargs['sub']}.{kwargs['audience']}"

    mcp = FastMCP("test")
    replog.register(mcp, s, recording_mint)
    return mcp, mints


@pytest.mark.asyncio
async def test_tool_returns_clean_error_when_identity_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """If the request scope doesn't carry an identity (OAuth disabled or
    JWT had no email), tools must return a tool-friendly error dict
    rather than raising into the transport."""
    mcp, mints = _build_test_setup(monkeypatch)

    # Build a Context whose request_context has no .request (the case
    # that triggers the early-return). We poke the tool's internal
    # closure by calling it via the FastMCP tool manager directly.
    fake_ctx = MagicMock()
    fake_ctx.request_context.request = None

    # The tool function under test is registered as a closure inside
    # replog.register(); reach it by name on the tool manager.
    tool = mcp._tool_manager.get_tool("replog_get_dashboard")
    assert tool is not None
    result = await tool.fn(ctx=fake_ctx)  # type: ignore[call-arg]
    assert isinstance(result, dict)
    assert result.get("error") == "missing identity"
    assert mints == [], "mint should not have been called on identity failure"
