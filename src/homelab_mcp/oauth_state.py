"""State store for the OAuth 2.1 authorization server.

What lives here:

  - **Registered clients** (from DCR). Persisted to SQLite when a
    `db_path` is supplied so Claude's `client_id` survives a restart;
    otherwise in-memory (Claude re-registers, costing one round trip).

  - **Refresh tokens**. Persisted to SQLite (as SHA-256 hashes, never
    plaintext) so a client can silently renew an expired access token
    after a service restart instead of re-running the interactive login.
    Rotated one-shot on use.

  - **Authorization codes**. One-shot, ~60s TTL, enforced server-side.
    A code carries everything needed to mint a JWT at the token endpoint:
    user identity, requested client, requested scope, the PKCE challenge
    Claude sent us, and the redirect_uri Claude said it'd accept (so we
    can reject token-endpoint hits that present a different one).
    Always in-memory — too short-lived to be worth persisting.

  - **Pending PocketID round-trips**. Short-lived dict keyed by the
    random `state` parameter we send to PocketID, holding the original
    Claude request so we can resume it after PocketID's callback.
    Always in-memory — losing one only aborts an in-flight login.

All operations are guarded by a single asyncio.Lock — uvicorn runs one
event loop so this is sufficient. The SQLite handle is opened with
`check_same_thread=False` and only ever touched while holding that lock,
so the single-connection-across-coroutines access stays serialized.
Cleanup of expired entries happens opportunistically on every access.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import secrets
import sqlite3
import time
from dataclasses import dataclass, field

log = logging.getLogger(__name__)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS oauth_client (
    client_id     TEXT PRIMARY KEY,
    client_secret TEXT NOT NULL,
    redirect_uris TEXT NOT NULL,
    client_name   TEXT NOT NULL,
    auth_method   TEXT NOT NULL,
    created_at    REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS refresh_token (
    token_hash  TEXT PRIMARY KEY,
    client_id   TEXT NOT NULL,
    user_email  TEXT NOT NULL,
    scope       TEXT,
    expires_at  REAL NOT NULL,
    family_id   TEXT
);
CREATE INDEX IF NOT EXISTS idx_refresh_expiry ON refresh_token(expires_at);
CREATE INDEX IF NOT EXISTS idx_refresh_family ON refresh_token(family_id);
-- Tombstones of already-rotated refresh tokens, kept so a replay of a
-- consumed token is distinguishable from an unknown one and can trigger
-- reuse detection (revoke the whole rotation family). Pruned by age.
CREATE TABLE IF NOT EXISTS consumed_refresh (
    token_hash  TEXT PRIMARY KEY,
    family_id   TEXT NOT NULL,
    consumed_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_consumed_at ON consumed_refresh(consumed_at);
"""


def _sha256(token: str) -> str:
    """Hash a bearer/refresh token for at-rest storage (never store plaintext)."""
    return hashlib.sha256(token.encode("ascii")).hexdigest()


def _now() -> float:
    """Monotonic-ish wall clock used for TTL comparisons."""
    return time.time()


def _new_token(length: int = 32) -> str:
    """URL-safe random token used for codes, client_ids, client_secrets, etc."""
    return secrets.token_urlsafe(length)


def _log_reuse(family_id: str, revoked_count: int) -> None:
    """Emit a security warning when a rotated refresh token is replayed."""
    log.warning(
        "refresh-token reuse detected — revoked %d live token(s) in family %s… "
        "(a rotated token was replayed; the chain may have leaked)",
        revoked_count,
        family_id[:8],
    )


def _migrate_refresh_family(conn: sqlite3.Connection) -> None:
    """Add the `family_id` column to a pre-existing refresh_token table.

    Databases created before reuse-detection was added lack the column.
    `CREATE TABLE IF NOT EXISTS` won't alter an existing table, so we add
    it explicitly. Legacy rows keep a NULL family_id; `consume_refresh`
    treats a NULL family as the token's own singleton family, so old
    tokens still rotate correctly (they just can't cross-revoke siblings
    they never had).
    """
    cols = {row[1] for row in conn.execute("PRAGMA table_info(refresh_token)")}
    if "family_id" not in cols:
        conn.execute("ALTER TABLE refresh_token ADD COLUMN family_id TEXT")


@dataclass
class RegisteredClient:
    """A client registered via RFC 7591 DCR."""

    client_id: str
    client_secret: str  # empty string for public clients (Claude is confidential)
    redirect_uris: list[str]
    client_name: str
    created_at: float
    # token_endpoint_auth_method per RFC 7591 — we accept "none" (PKCE only)
    # or "client_secret_post" (Claude's preferred path).
    token_endpoint_auth_method: str = "client_secret_post"


@dataclass
class PendingAuthorization:
    """State stored while we redirect to PocketID and wait for the callback.

    Keyed by the random `state` we send to PocketID; the upstream
    callback brings it back so we can resume the original Claude request.
    """

    # Claude's original /authorize parameters
    client_id: str
    redirect_uri: str
    code_challenge: str
    code_challenge_method: str
    claude_state: str | None
    scope: str | None
    # Our own randomness so we can later assert this wasn't tampered with
    pocketid_code_verifier: str
    pocketid_nonce: str
    expires_at: float


@dataclass
class IssuedAuthorizationCode:
    """An authorization code we issued to Claude after PocketID login succeeded.

    Single-use; the token endpoint deletes it on exchange.
    """

    client_id: str
    redirect_uri: str
    code_challenge: str
    code_challenge_method: str
    user_email: str
    expires_at: float
    scope: str | None = None


@dataclass
class IssuedRefreshToken:
    """A refresh token we handed Claude alongside an access token.

    Rotated on use: the token endpoint consumes the presented refresh
    token (one-shot) and issues a fresh one with the same identity. This
    lets a long-lived client renew short-lived access tokens silently,
    without re-running the interactive PocketID login.

    Every token in a rotation chain shares a `family_id`. If an
    already-consumed token is ever presented again (a replay), the whole
    family is revoked — see `OAuthState.consume_refresh`.
    """

    client_id: str
    user_email: str
    expires_at: float
    scope: str | None = None
    family_id: str = ""


@dataclass
class OAuthState:
    """OAuth state store. One instance per process.

    All public methods are async and acquire `_lock` internally so callers
    don't have to think about concurrency. When `_db` is set, registered
    clients and refresh tokens are persisted to SQLite (surviving restarts);
    otherwise they live in the in-memory dicts. Pending round-trips and
    authorization codes are always in-memory regardless.
    """

    _lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    _clients: dict[str, RegisteredClient] = field(default_factory=dict)
    _pending: dict[str, PendingAuthorization] = field(default_factory=dict)
    _codes: dict[str, IssuedAuthorizationCode] = field(default_factory=dict)
    _refresh: dict[str, IssuedRefreshToken] = field(default_factory=dict)
    # In-memory reuse-detection tombstones: sha256(token) -> family_id.
    _consumed_refresh: dict[str, str] = field(default_factory=dict)
    _db: sqlite3.Connection | None = None
    _client_retention_seconds: float | None = None
    _consumed_retention_seconds: float | None = None

    @classmethod
    def open(
        cls,
        db_path: str | None,
        *,
        client_retention_seconds: float | None = None,
        consumed_retention_seconds: float | None = None,
    ) -> OAuthState:
        """Construct a store, optionally backed by a SQLite file at `db_path`.

        Pass `None` (or an empty string) for a pure in-memory store — used
        by tests and OAuth-disabled local dev. Any other value is treated
        as a SQLite path (`:memory:` works too, though it won't survive a
        restart). The schema is created on first open.

        `client_retention_seconds` bounds how long an abandoned persisted
        client (no live refresh token) is kept; pruned opportunistically on
        registration and via `run_startup_maintenance`. `None` disables
        pruning (used by the in-memory store, which has nothing to prune).

        `consumed_retention_seconds` bounds how long a rotated refresh
        token's reuse-detection tombstone is kept (set it to the refresh
        token lifetime — a replay is only meaningful while the family could
        still be live). `None` keeps them for the process lifetime.
        """
        if not db_path:
            return cls(_consumed_retention_seconds=consumed_retention_seconds)
        conn = sqlite3.connect(db_path, check_same_thread=False)
        conn.execute("PRAGMA journal_mode=WAL")
        conn.executescript(_SCHEMA)
        _migrate_refresh_family(conn)
        conn.commit()
        return cls(
            _db=conn,
            _client_retention_seconds=client_retention_seconds,
            _consumed_retention_seconds=consumed_retention_seconds,
        )

    def _delete_stale_locked(self) -> int:
        """Prune expired refresh tokens + abandoned clients. Caller must hold
        the lock (or be the single-threaded startup path).

        A client is "abandoned" if it's older than the retention window AND
        holds no refresh token — an in-use client always has a live (or at
        least not-yet-pruned) refresh token, so this never reaps active
        connectors. Returns the number of client rows removed.
        """
        if self._db is None:
            return 0
        self._db.execute("DELETE FROM refresh_token WHERE expires_at < ?", (_now(),))
        if self._consumed_retention_seconds is not None:
            self._db.execute(
                "DELETE FROM consumed_refresh WHERE consumed_at < ?",
                (_now() - self._consumed_retention_seconds,),
            )
        removed = 0
        if self._client_retention_seconds is not None:
            cutoff = _now() - self._client_retention_seconds
            cur = self._db.execute(
                "DELETE FROM oauth_client WHERE created_at < ? "
                "AND client_id NOT IN (SELECT client_id FROM refresh_token)",
                (cutoff,),
            )
            removed = cur.rowcount or 0
        self._db.commit()
        return removed

    def run_startup_maintenance(self) -> int:
        """One-shot synchronous cleanup at boot (no event loop running yet).

        Safe to call before uvicorn starts serving because nothing else
        touches the connection at that point. Returns clients pruned.
        """
        return self._delete_stale_locked()

    # ── Clients (DCR) ────────────────────────────────────────────────
    async def register_client(
        self,
        *,
        redirect_uris: list[str],
        client_name: str,
        token_endpoint_auth_method: str,
    ) -> RegisteredClient:
        async with self._lock:
            # Opportunistic GC: registration is rare, so this keeps the
            # persisted client table from growing without a restart.
            self._delete_stale_locked()
            client_id = _new_token(24)
            # Public clients (auth method = "none") get no secret.
            secret = "" if token_endpoint_auth_method == "none" else _new_token(40)
            client = RegisteredClient(
                client_id=client_id,
                client_secret=secret,
                redirect_uris=list(redirect_uris),
                client_name=client_name,
                created_at=_now(),
                token_endpoint_auth_method=token_endpoint_auth_method,
            )
            if self._db is not None:
                self._db.execute(
                    "INSERT INTO oauth_client VALUES (?, ?, ?, ?, ?, ?)",
                    (
                        client.client_id,
                        client.client_secret,
                        json.dumps(client.redirect_uris),
                        client.client_name,
                        client.token_endpoint_auth_method,
                        client.created_at,
                    ),
                )
                self._db.commit()
            else:
                self._clients[client_id] = client
            return client

    async def get_client(self, client_id: str) -> RegisteredClient | None:
        async with self._lock:
            if self._db is None:
                return self._clients.get(client_id)
            row = self._db.execute(
                "SELECT client_id, client_secret, redirect_uris, client_name, "
                "auth_method, created_at FROM oauth_client WHERE client_id = ?",
                (client_id,),
            ).fetchone()
            if row is None:
                return None
            return RegisteredClient(
                client_id=row[0],
                client_secret=row[1],
                redirect_uris=list(json.loads(row[2])),
                client_name=row[3],
                token_endpoint_auth_method=row[4],
                created_at=row[5],
            )

    # ── Pending PocketID round-trips ─────────────────────────────────
    async def create_pending(self, state_token: str, pending: PendingAuthorization) -> None:
        async with self._lock:
            self._prune_locked(self._pending)
            self._pending[state_token] = pending

    async def pop_pending(self, state_token: str) -> PendingAuthorization | None:
        async with self._lock:
            self._prune_locked(self._pending)
            return self._pending.pop(state_token, None)

    # ── Issued authorization codes ───────────────────────────────────
    async def store_code(self, code: str, payload: IssuedAuthorizationCode) -> None:
        async with self._lock:
            self._prune_locked(self._codes)
            self._codes[code] = payload

    async def consume_code(self, code: str) -> IssuedAuthorizationCode | None:
        """Atomically retrieve + delete an authorization code (one-shot)."""
        async with self._lock:
            self._prune_locked(self._codes)
            return self._codes.pop(code, None)

    # ── Refresh tokens (rotated on use) ──────────────────────
    async def store_refresh(self, token: str, payload: IssuedRefreshToken) -> None:
        """Persist a freshly-issued refresh token.

        `payload.family_id` MUST be set by the caller: a new value for the
        first token of a chain (authorization_code grant) and the parent's
        value for a rotated token (refresh_token grant).
        """
        async with self._lock:
            if self._db is not None:
                self._db.execute(
                    "INSERT INTO refresh_token "
                    "(token_hash, client_id, user_email, scope, expires_at, family_id) "
                    "VALUES (?, ?, ?, ?, ?, ?)",
                    (
                        _sha256(token),
                        payload.client_id,
                        payload.user_email,
                        payload.scope,
                        payload.expires_at,
                        payload.family_id,
                    ),
                )
                self._db.execute("DELETE FROM refresh_token WHERE expires_at < ?", (_now(),))
                self._db.commit()
            else:
                self._prune_locked(self._refresh)
                self._refresh[token] = payload

    async def consume_refresh(self, token: str) -> IssuedRefreshToken | None:
        """Atomically retrieve + delete a refresh token (one-shot, rotated).

        Reuse detection (OAuth 2.1 / RFC 9700): when a token that is not
        active is presented, we check the consumed-token tombstones. A hit
        means an already-rotated token was replayed — evidence the chain
        leaked — so we revoke the entire rotation family (invalidating the
        legitimate client's live descendant too) and return None. A miss is
        an ordinary unknown token. Either way the caller sees None and
        returns `invalid_grant`.
        """
        token_hash = _sha256(token)
        async with self._lock:
            if self._db is not None:
                row = self._db.execute(
                    "SELECT client_id, user_email, scope, expires_at, family_id "
                    "FROM refresh_token WHERE token_hash = ?",
                    (token_hash,),
                ).fetchone()
                if row is None:
                    self._detect_reuse_locked(token_hash)
                    return None
                family_id = row[4] or token_hash  # legacy rows: singleton family
                self._db.execute("DELETE FROM refresh_token WHERE token_hash = ?", (token_hash,))
                self._db.execute(
                    "INSERT OR REPLACE INTO consumed_refresh "
                    "(token_hash, family_id, consumed_at) VALUES (?, ?, ?)",
                    (token_hash, family_id, _now()),
                )
                self._db.commit()
                return IssuedRefreshToken(
                    client_id=row[0],
                    user_email=row[1],
                    scope=row[2],
                    expires_at=row[3],
                    family_id=family_id,
                )
            # In-memory path.
            self._prune_locked(self._refresh)
            payload = self._refresh.pop(token, None)
            if payload is None:
                self._detect_reuse_locked(token_hash)
                return None
            family_id = payload.family_id or token_hash
            self._consumed_refresh[token_hash] = family_id
            payload.family_id = family_id
            return payload

    def _detect_reuse_locked(self, token_hash: str) -> None:
        """Revoke a token's whole family if the token is a replayed tombstone.

        Caller holds the lock. No-op for a genuinely unknown token.
        """
        if self._db is not None:
            row = self._db.execute(
                "SELECT family_id FROM consumed_refresh WHERE token_hash = ?",
                (token_hash,),
            ).fetchone()
            if row is None:
                return
            family_id = row[0]
            cur = self._db.execute("DELETE FROM refresh_token WHERE family_id = ?", (family_id,))
            self._db.commit()
            _log_reuse(family_id, cur.rowcount or 0)
            return
        family_id = self._consumed_refresh.get(token_hash)
        if family_id is None:
            return
        revoked = [t for t, p in self._refresh.items() if (p.family_id or "") == family_id]
        for t in revoked:
            del self._refresh[t]
        _log_reuse(family_id, len(revoked))

    # ── Cleanup ─────────────────────────────────────────────
    @staticmethod
    def _prune_locked(
        store: dict[str, PendingAuthorization]
        | dict[str, IssuedAuthorizationCode]
        | dict[str, IssuedRefreshToken],
    ) -> None:
        """Remove expired entries from a dict. Caller MUST hold the lock."""
        now = _now()
        expired = [k for k, v in store.items() if v.expires_at < now]
        for k in expired:
            del store[k]
