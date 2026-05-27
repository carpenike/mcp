"""In-memory state for the OAuth 2.1 authorization server.

What lives here:

  - **Registered clients** (from DCR). Survives only while the process runs.
    Claude re-registers if the connector is added again after a restart,
    which costs ~one extra round trip. No persistence needed for v1.

  - **Authorization codes**. One-shot, ~60s TTL, enforced server-side.
    A code carries everything needed to mint a JWT at the token endpoint:
    user identity, requested client, requested scope, the PKCE challenge
    Claude sent us, and the redirect_uri Claude said it'd accept (so we
    can reject token-endpoint hits that present a different one).

  - **Pending PocketID round-trips**. Short-lived dict keyed by the
    random `state` parameter we send to PocketID, holding the original
    Claude request so we can resume it after PocketID's callback.

All operations are guarded by a single asyncio.Lock — uvicorn runs one
event loop so this is sufficient. Cleanup of expired entries happens
opportunistically on every access.
"""

from __future__ import annotations

import asyncio
import secrets
import time
from dataclasses import dataclass, field


def _now() -> float:
    """Monotonic-ish wall clock used for TTL comparisons."""
    return time.time()


def _new_token(length: int = 32) -> str:
    """URL-safe random token used for codes, client_ids, client_secrets, etc."""
    return secrets.token_urlsafe(length)


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
class OAuthState:
    """Whole in-memory store. One instance per process.

    All public methods are async and acquire `_lock` internally so callers
    don't have to think about concurrency.
    """

    _lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    _clients: dict[str, RegisteredClient] = field(default_factory=dict)
    _pending: dict[str, PendingAuthorization] = field(default_factory=dict)
    _codes: dict[str, IssuedAuthorizationCode] = field(default_factory=dict)

    # ── Clients (DCR) ────────────────────────────────────────────────
    async def register_client(
        self,
        *,
        redirect_uris: list[str],
        client_name: str,
        token_endpoint_auth_method: str,
    ) -> RegisteredClient:
        async with self._lock:
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
            self._clients[client_id] = client
            return client

    async def get_client(self, client_id: str) -> RegisteredClient | None:
        async with self._lock:
            return self._clients.get(client_id)

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

    # ── Cleanup ──────────────────────────────────────────────────────
    @staticmethod
    def _prune_locked(
        store: dict[str, PendingAuthorization] | dict[str, IssuedAuthorizationCode],
    ) -> None:
        """Remove expired entries from a dict. Caller MUST hold the lock."""
        now = _now()
        expired = [k for k, v in store.items() if v.expires_at < now]
        for k in expired:
            del store[k]
