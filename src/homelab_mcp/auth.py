"""JWT authentication middleware for homelab-mcp.

We are our own OAuth 2.1 Authorization Server (see oauth_provider.py).
Every access token we mint is an RS256 JWT signed by the local signing
key. This middleware validates incoming tokens against the *public* side
of that key, in-process, with no network calls.

Tokens are accepted from `Authorization: Bearer <jwt>`. Cloudflare's
legacy `Cf-Access-Jwt-Assertion` header is no longer accepted — we
removed the CF Access integration.

Any request missing/expired/mis-signed/wrong-audience gets a 401 with a
JSON error body that follows the OAuth 2.0 Bearer Token spec (RFC 6750).
Non-HTTP scopes (lifespan, websocket) pass through untouched.

Allowlisted paths (RFC 9728 protected-resource metadata, RFC 8414
authorization-server metadata, the OAuth endpoints themselves) are
passed through unauthenticated so MCP clients can discover the AS and
complete a login before they have a token.
"""

from __future__ import annotations

import json
import logging
from typing import Any

import jwt
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.rsa import RSAPrivateKey
from starlette.types import ASGIApp, Receive, Scope, Send

from homelab_mcp.signing_key import SigningKey

log = logging.getLogger(__name__)


class JWTAuthMiddleware:
    """ASGI middleware that requires a valid homelab-mcp-issued JWT on every HTTP request.

    On success, the decoded claims are stashed at `scope["user"]` so tool
    handlers can access the caller's identity (e.g. for per-user gating).

    On failure, returns 401 immediately without invoking the wrapped app.
    """

    # Paths that must be reachable WITHOUT authentication so the OAuth
    # discovery + interactive flow can complete. Keep this list minimal.
    UNAUTHENTICATED_PATHS: frozenset[str] = frozenset(
        {
            "/.well-known/oauth-protected-resource",
            "/.well-known/oauth-authorization-server",
            "/oauth/jwks.json",
            "/oauth/register",
            "/oauth/authorize",
            "/oauth/callback",
            "/oauth/token",
        }
    )

    def __init__(
        self,
        app: ASGIApp,
        *,
        signing_key: SigningKey,
        issuer: str,
        audience: str,
        resource_metadata_url: str | None = None,
        extra_unauthenticated_paths: frozenset[str] = frozenset(),
    ) -> None:
        self.app = app
        self.issuer = issuer
        self.audience = audience
        # Absolute URL of the (path-suffixed) RFC 9728 protected-resource
        # metadata doc. Emitted in the WWW-Authenticate header on every 401
        # so spec-strict clients (VS Code) can discover the AS per §5.3.
        self.resource_metadata_url = resource_metadata_url
        # Allowlist the PRM path itself so the unauthenticated discovery
        # fetch isn't challenged. The origin-root + AS-metadata + OAuth
        # paths are static (UNAUTHENTICATED_PATHS); the path-suffixed PRM
        # is derived from mcp_path so we add it per-instance.
        #
        # `extra_unauthenticated_paths` carries other public, bearer-free
        # routes (e.g. the hosted contract docs) that must stay outside the
        # OAuth/JWT path entirely.
        self._unauthenticated_paths: frozenset[str] = (
            self.UNAUTHENTICATED_PATHS | extra_unauthenticated_paths
        )
        if resource_metadata_url is not None:
            suffixed_path = resource_metadata_url[len(issuer) :]
            self._unauthenticated_paths = self._unauthenticated_paths | {suffixed_path}
        # Derive the public key once. We need the cryptography object, not
        # the JWK dict, because PyJWT takes the raw key.
        private: RSAPrivateKey = serialization.load_pem_private_key(  # type: ignore[assignment]
            signing_key.private_pem, password=None
        )
        self._public_key = private.public_key()
        self._kid = signing_key.kid

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        if scope.get("path", "") in self._unauthenticated_paths:
            await self.app(scope, receive, send)
            return

        token = self._extract_token(scope)
        if not token:
            await self._respond_401(
                send,
                "missing bearer token",
                www_authenticate=self._www_authenticate("missing bearer token"),
            )
            return

        try:
            claims = self._validate(token)
        except jwt.InvalidTokenError as e:
            log.warning("JWT rejected: %s", e)
            await self._respond_401(
                send,
                f"invalid token: {e}",
                www_authenticate=self._www_authenticate(f"invalid token: {e}"),
            )
            return

        # Stash claims so tool handlers can read scope["user"]["email"] etc.
        scope_user: dict[str, Any] = dict(scope.get("user") or {})
        scope_user["email"] = claims.get("email")
        scope_user["claims"] = claims
        scope["user"] = scope_user

        await self.app(scope, receive, send)

    def _www_authenticate(self, reason: str) -> bytes:
        """Build the WWW-Authenticate header value (RFC 6750 + RFC 9728 §5.3).

        Includes `resource_metadata` so spec-strict clients (VS Code)
        discover the AS from the 401 rather than guessing well-known paths.
        """
        parts = [
            'Bearer realm="homelab-mcp"',
            'error="invalid_token"',
            f'error_description="{reason}"',
        ]
        if self.resource_metadata_url is not None:
            parts.append(f'resource_metadata="{self.resource_metadata_url}"')
        return ", ".join(parts).encode("ascii")

    @staticmethod
    def _extract_token(scope: Scope) -> str | None:
        """Pull the JWT from `Authorization: Bearer ...`."""
        headers: dict[bytes, bytes] = {k.lower(): v for k, v in scope.get("headers", [])}
        auth = headers.get(b"authorization", b"").decode("ascii", errors="ignore")
        if auth.lower().startswith("bearer "):
            return auth[7:].strip()
        return None

    def _validate(self, token: str) -> dict[str, Any]:
        """Decode + verify the JWT against the local public key. Raises on any failure."""
        unverified = jwt.get_unverified_header(token)
        kid = unverified.get("kid")
        if kid != self._kid:
            raise jwt.InvalidTokenError(f"unknown kid: {kid!r}")

        decoded: dict[str, Any] = jwt.decode(
            token,
            self._public_key,
            algorithms=["RS256"],
            issuer=self.issuer,
            audience=self.audience,
            options={"require": ["exp", "iat", "iss", "aud", "sub"]},
        )
        return decoded

    @staticmethod
    async def _respond_401(
        send: Send,
        reason: str,
        *,
        www_authenticate: bytes = b'Bearer realm="homelab-mcp"',
    ) -> None:
        """Send a JSON 401 response and end the request."""
        body = json.dumps({"error": "unauthorized", "reason": reason}).encode()
        await send(
            {
                "type": "http.response.start",
                "status": 401,
                "headers": [
                    (b"content-type", b"application/json"),
                    (b"www-authenticate", www_authenticate),
                    (b"content-length", str(len(body)).encode()),
                ],
            }
        )
        await send({"type": "http.response.body", "body": body})
