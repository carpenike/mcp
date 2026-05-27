"""Cloudflare Access JWT validation.

Cloudflare Access (in either reverse-proxy or "Access for SaaS (OIDC)"
mode) signs every token with per-team RSA keys published at
`https://<team>.cloudflareaccess.com/cdn-cgi/access/certs`. We cache
those keys for an hour and validate every incoming HTTP request before
forwarding it to the MCP transport handler.

Tokens are accepted from either:
  - `Authorization: Bearer <jwt>` (the MCP custom-connector path)
  - `Cf-Access-Jwt-Assertion: <jwt>` (the reverse-proxy header path)

Any request missing/expired/mis-signed/wrong-audience gets a 401 with a
JSON error body. Non-HTTP scopes (lifespan, websocket) pass through
untouched.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from dataclasses import dataclass, field
from typing import Any

import httpx
import jwt
from starlette.types import ASGIApp, Receive, Scope, Send

log = logging.getLogger(__name__)


@dataclass
class JWKSCache:
    """Cache of the team's JSON Web Key Set, refreshed every `ttl` seconds.

    Thread-safety via asyncio.Lock — uvicorn runs one event loop so this
    is sufficient.
    """

    jwks_url: str
    ttl: float = 3600.0
    _cache: dict[str, Any] = field(default_factory=dict)
    _expires: float = 0.0
    _lock: asyncio.Lock = field(default_factory=asyncio.Lock)

    async def get(self) -> dict[str, Any]:
        """Return the current JWKS, refreshing from upstream if stale."""
        async with self._lock:
            now = time.monotonic()
            if now < self._expires and self._cache:
                return self._cache
            log.debug("refreshing JWKS from %s", self.jwks_url)
            async with httpx.AsyncClient(timeout=10) as client:
                resp = await client.get(self.jwks_url)
                resp.raise_for_status()
                self._cache = resp.json()
                self._expires = now + self.ttl
            log.info(
                "JWKS refreshed: %d keys (kids=%s)",
                len(self._cache.get("keys", [])),
                ",".join(k.get("kid", "?") for k in self._cache.get("keys", [])),
            )
            return self._cache


class JWTAuthMiddleware:
    """ASGI middleware that requires a valid Cloudflare Access JWT on every HTTP request.

    On success, the decoded claims are stashed at `scope["user"]` so tool
    handlers can access the caller's identity (e.g. for per-user gating).

    On failure, returns 401 immediately without invoking the wrapped app.

    Allowlisted paths (RFC 9728 protected-resource metadata, RFC 8414
    authorization-server metadata aliases) are passed through unauthenticated
    so MCP clients can discover the upstream authorization server before
    they have a token. These docs contain no secrets.
    """

    # Paths that must be reachable WITHOUT authentication so the OAuth
    # discovery dance can complete. Keep this list minimal.
    UNAUTHENTICATED_PATHS: frozenset[str] = frozenset(
        {
            "/.well-known/oauth-protected-resource",
            "/.well-known/oauth-authorization-server",
        }
    )

    def __init__(
        self,
        app: ASGIApp,
        *,
        jwks_cache: JWKSCache,
        issuer: str,
        audience: str,
    ) -> None:
        self.app = app
        self.jwks_cache = jwks_cache
        self.issuer = issuer
        self.audience = audience

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        if scope.get("path", "") in self.UNAUTHENTICATED_PATHS:
            await self.app(scope, receive, send)
            return

        token = self._extract_token(scope)
        if not token:
            await self._respond_401(send, "missing bearer token or CF Access header")
            return

        try:
            claims = await self._validate(token)
        except jwt.InvalidTokenError as e:
            log.warning("JWT rejected: %s", e)
            await self._respond_401(send, f"invalid token: {e}")
            return

        # Stash claims so tool handlers can read scope["user"]["email"] etc.
        scope_user: dict[str, Any] = dict(scope.get("user") or {})
        scope_user["email"] = claims.get("email")
        scope_user["claims"] = claims
        scope["user"] = scope_user

        await self.app(scope, receive, send)

    @staticmethod
    def _extract_token(scope: Scope) -> str | None:
        """Pull the JWT from `Authorization: Bearer ...` or `Cf-Access-Jwt-Assertion`."""
        headers: dict[bytes, bytes] = {k.lower(): v for k, v in scope.get("headers", [])}

        auth = headers.get(b"authorization", b"").decode("ascii", errors="ignore")
        if auth.lower().startswith("bearer "):
            return auth[7:].strip()

        cf = headers.get(b"cf-access-jwt-assertion", b"").decode("ascii", errors="ignore")
        if cf:
            return cf.strip()

        return None

    async def _validate(self, token: str) -> dict[str, Any]:
        """Decode + verify the JWT against the team's JWKS. Raises on any failure."""
        unverified = jwt.get_unverified_header(token)
        kid = unverified.get("kid")
        if not kid:
            raise jwt.InvalidTokenError("JWT header missing 'kid'")

        jwks = await self.jwks_cache.get()
        key_data = next(
            (k for k in jwks.get("keys", []) if k.get("kid") == kid),
            None,
        )
        if key_data is None:
            raise jwt.InvalidTokenError(f"unknown kid: {kid}")

        public_key = jwt.PyJWK(key_data).key
        decoded: dict[str, Any] = jwt.decode(
            token,
            public_key,
            algorithms=[unverified.get("alg", "RS256")],
            issuer=self.issuer,
            audience=self.audience,
            options={"require": ["exp", "iat", "iss", "aud"]},
        )
        return decoded

    @staticmethod
    async def _respond_401(send: Send, reason: str) -> None:
        """Send a JSON 401 response and end the request."""
        body = json.dumps({"error": "unauthorized", "reason": reason}).encode()
        await send(
            {
                "type": "http.response.start",
                "status": 401,
                "headers": [
                    (b"content-type", b"application/json"),
                    (b"www-authenticate", b'Bearer realm="homelab-mcp"'),
                    (b"content-length", str(len(body)).encode()),
                ],
            }
        )
        await send({"type": "http.response.body", "body": body})
