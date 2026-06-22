"""SQLite-backed persistence for OAuthState.

Verifies that registered clients and refresh tokens survive a simulated
restart (a fresh OAuthState.open() against the same file), that refresh
tokens rotate one-shot, that expired tokens are pruned, and that raw
tokens are never written to disk (only their SHA-256 hash).

Pending PocketID round-trips and authorization codes are intentionally
in-memory only, so they are NOT expected to persist — that's covered by
the in-memory paths exercised in test_oauth_flow.py.
"""

from __future__ import annotations

import hashlib
import time
from pathlib import Path

import pytest

from homelab_mcp.oauth_state import IssuedRefreshToken, OAuthState


@pytest.fixture
def db_path(tmp_path: Path) -> str:
    return str(tmp_path / "state.db")


async def test_client_persists_across_restart(db_path: str) -> None:
    store = OAuthState.open(db_path)
    client = await store.register_client(
        redirect_uris=["https://claude.ai/api/mcp/auth_callback"],
        client_name="Claude",
        token_endpoint_auth_method="client_secret_post",
    )

    # Simulate a process restart: brand-new store over the same file.
    reopened = OAuthState.open(db_path)
    got = await reopened.get_client(client.client_id)
    assert got is not None
    assert got.client_id == client.client_id
    assert got.client_secret == client.client_secret
    assert got.redirect_uris == ["https://claude.ai/api/mcp/auth_callback"]
    assert got.client_name == "Claude"
    assert got.token_endpoint_auth_method == "client_secret_post"


async def test_refresh_token_persists_and_rotates(db_path: str) -> None:
    store = OAuthState.open(db_path)
    await store.store_refresh(
        "refresh-abc",
        IssuedRefreshToken(
            client_id="cid",
            user_email="user@example.com",
            scope="openid email",
            expires_at=time.time() + 3600,
        ),
    )

    # Survives a restart.
    reopened = OAuthState.open(db_path)
    consumed = await reopened.consume_refresh("refresh-abc")
    assert consumed is not None
    assert consumed.client_id == "cid"
    assert consumed.user_email == "user@example.com"
    assert consumed.scope == "openid email"

    # One-shot: a second consume (even from another instance) fails.
    again = OAuthState.open(db_path)
    assert await again.consume_refresh("refresh-abc") is None


async def test_expired_refresh_token_rejected(db_path: str) -> None:
    store = OAuthState.open(db_path)
    await store.store_refresh(
        "refresh-old",
        IssuedRefreshToken(
            client_id="cid",
            user_email="user@example.com",
            scope=None,
            expires_at=time.time() - 1,  # already expired
        ),
    )
    # store_refresh prunes expired rows on write; consume must not return it.
    assert await store.consume_refresh("refresh-old") is None


async def test_raw_token_never_written_to_disk(db_path: str) -> None:
    store = OAuthState.open(db_path)
    raw = "super-secret-refresh-token"
    await store.store_refresh(
        raw,
        IssuedRefreshToken(
            client_id="cid",
            user_email="user@example.com",
            scope=None,
            expires_at=time.time() + 3600,
        ),
    )

    # The raw token must not appear anywhere in the DB file; its hash must.
    # WAL mode keeps recent writes in a `-wal` sidecar until checkpoint, so
    # scan every file backing the database, not just the main one.
    db = Path(db_path)
    blob = b"".join(
        p.read_bytes()  # noqa: ASYNC240 - test reads tiny local files
        for p in [db, *db.parent.glob(db.name + "-*")]
        if p.exists()
    )
    assert raw.encode() not in blob
    assert hashlib.sha256(raw.encode()).hexdigest().encode() in blob


async def test_in_memory_store_has_no_db(tmp_path: Path) -> None:
    # Empty/None path → pure in-memory; nothing is written.
    store = OAuthState.open(None)
    client = await store.register_client(
        redirect_uris=["https://claude.ai/cb"],
        client_name="Claude",
        token_endpoint_auth_method="none",
    )
    assert await store.get_client(client.client_id) is not None
    # A separate in-memory store shares no state.
    other = OAuthState.open(None)
    assert await other.get_client(client.client_id) is None


async def test_startup_maintenance_prunes_abandoned_clients(db_path: str) -> None:
    store = OAuthState.open(db_path, client_retention_seconds=30 * 86400)

    # Register all clients first; opportunistic pruning during registration
    # only acts on rows already past retention, so registering up front lets
    # us control exactly which rows are "old" before invoking maintenance.
    abandoned = await store.register_client(
        redirect_uris=["https://claude.ai/cb"],
        client_name="Old",
        token_endpoint_auth_method="none",
    )
    active = await store.register_client(
        redirect_uris=["https://claude.ai/cb"],
        client_name="Active",
        token_endpoint_auth_method="none",
    )
    recent = await store.register_client(
        redirect_uris=["https://claude.ai/cb"],
        client_name="Recent",
        token_endpoint_auth_method="none",
    )
    # The active client still holds a live refresh token → must be kept even
    # though it's old.
    await store.store_refresh(
        "live-token",
        IssuedRefreshToken(
            client_id=active.client_id,
            user_email="user@example.com",
            scope=None,
            expires_at=time.time() + 3600,
        ),
    )

    # Backdate the abandoned + active clients past the retention window.
    assert store._db is not None
    old = time.time() - 90 * 86400
    store._db.execute(
        "UPDATE oauth_client SET created_at = ? WHERE client_id IN (?, ?)",
        (old, abandoned.client_id, active.client_id),
    )
    store._db.commit()

    removed = store.run_startup_maintenance()
    assert removed == 1
    assert await store.get_client(abandoned.client_id) is None
    assert await store.get_client(active.client_id) is not None
    assert await store.get_client(recent.client_id) is not None


async def test_maintenance_drops_expired_refresh_tokens(db_path: str) -> None:
    store = OAuthState.open(db_path, client_retention_seconds=30 * 86400)
    await store.store_refresh(
        "expired",
        IssuedRefreshToken(
            client_id="cid",
            user_email="user@example.com",
            scope=None,
            expires_at=time.time() - 1,
        ),
    )
    store.run_startup_maintenance()
    assert store._db is not None
    count = store._db.execute("SELECT COUNT(*) FROM refresh_token").fetchone()[0]
    assert count == 0
