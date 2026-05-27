"""OAuth 2.1 authorization server for homelab-mcp.

This module exposes the HTTP routes that implement the AS side of the
spec:

  GET  /.well-known/oauth-authorization-server  → RFC 8414 metadata
  GET  /oauth/jwks.json                         → RFC 7517 public keys
  POST /oauth/register                          → RFC 7591 DCR
  GET  /oauth/authorize                         → start interactive flow
  GET  /oauth/callback                          → PocketID return leg
  POST /oauth/token                             → exchange code for JWT

The dance for an MCP custom-connector is:

  Claude  ──(1) POST /oauth/register──►  homelab-mcp
  Claude  ──(2) GET /oauth/authorize──►  homelab-mcp
                                           ├── stash state in cookie+memory
                                           └── 302 to PocketID's /authorize
  PocketID user logs in (passkey, etc.)
  PocketID ─(3) GET /oauth/callback──►  homelab-mcp
                                           ├── exchange code at PocketID
                                           ├── verify ID token
                                           ├── check user allowlist
                                           ├── mint our own auth code
                                           └── 302 to Claude's redirect_uri
  Claude  ──(4) POST /oauth/token────►  homelab-mcp
                                           ├── verify PKCE
                                           ├── consume code (one-shot)
                                           └── mint RS256 JWT
  Claude  ──(5) GET /mcp w/ Bearer──►  homelab-mcp (JWT middleware)
"""

from __future__ import annotations

import base64
import hashlib
import logging
import secrets
import time
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlencode

import httpx
from authlib.jose import JsonWebKey, JsonWebToken
from starlette.requests import Request
from starlette.responses import JSONResponse, RedirectResponse, Response
from starlette.routing import Route

from homelab_mcp.config import Settings
from homelab_mcp.oauth_state import (
    IssuedAuthorizationCode,
    OAuthState,
    PendingAuthorization,
)
from homelab_mcp.signing_key import SigningKey

log = logging.getLogger(__name__)

# PocketID OIDC discovery doc cache. Refreshed lazily per process.
_PROVIDER_METADATA_CACHE: dict[str, Any] = {}
_PROVIDER_METADATA_EXPIRY: float = 0.0
_PROVIDER_METADATA_TTL = 3600.0


@dataclass(frozen=True)
class _UpstreamMetadata:
    """The fields we pull from PocketID's discovery doc."""

    authorization_endpoint: str
    token_endpoint: str
    jwks_uri: str
    issuer: str


async def _load_upstream_metadata(settings: Settings) -> _UpstreamMetadata:
    """Fetch + cache PocketID's OIDC discovery doc.

    Cached in module scope (single-process server) to avoid hitting
    PocketID on every /authorize.
    """
    global _PROVIDER_METADATA_CACHE, _PROVIDER_METADATA_EXPIRY
    now = time.monotonic()
    if _PROVIDER_METADATA_CACHE and now < _PROVIDER_METADATA_EXPIRY:
        meta: dict[str, Any] = _PROVIDER_METADATA_CACHE
    else:
        url = settings.pocketid_issuer.rstrip("/") + "/.well-known/openid-configuration"
        log.info("Loading upstream OIDC metadata from %s", url)
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.get(url)
            resp.raise_for_status()
            meta = resp.json()
        _PROVIDER_METADATA_CACHE = meta
        _PROVIDER_METADATA_EXPIRY = now + _PROVIDER_METADATA_TTL
    return _UpstreamMetadata(
        authorization_endpoint=meta["authorization_endpoint"],
        token_endpoint=meta["token_endpoint"],
        jwks_uri=meta["jwks_uri"],
        issuer=meta["issuer"],
    )


async def _load_upstream_jwks(jwks_uri: str) -> dict[str, Any]:
    """Pull PocketID's JWKS for ID-token verification.

    Cheap operation: one HTTPS round-trip on every callback. We could
    cache it for an hour, but at one login per (rarely) it's not worth
    the failure mode of stale keys.
    """
    async with httpx.AsyncClient(timeout=10) as client:
        resp = await client.get(jwks_uri)
        resp.raise_for_status()
        data: dict[str, Any] = resp.json()
        return data


def _b64url_no_pad(data: bytes) -> str:
    """Standard PKCE / OAuth URL-safe base64 with no padding."""
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def _check_pkce(code_verifier: str, expected_challenge: str, method: str) -> bool:
    """Confirm the client's code_verifier matches the challenge it committed to."""
    if method == "S256":
        digest = hashlib.sha256(code_verifier.encode("ascii")).digest()
        return secrets.compare_digest(_b64url_no_pad(digest), expected_challenge)
    if method == "plain":
        # Allowed by RFC 7636 but not by OAuth 2.1; reject to stay strict.
        return False
    return False


def _redirect_allowed(uri: str, settings: Settings) -> bool:
    """Return True iff `uri` starts with one of the allowlisted prefixes."""
    return any(uri.startswith(p) for p in settings.oauth_redirect_uri_allowlist)


def _json_error(status: int, error: str, description: str) -> JSONResponse:
    """Return an OAuth-shaped error per RFC 6749 §5.2."""
    log.info("oauth-error %s: %s (%s)", status, error, description)
    return JSONResponse(
        {"error": error, "error_description": description},
        status_code=status,
    )


def build_routes(
    settings: Settings,
    signing_key: SigningKey,
    state: OAuthState,
) -> list[Route]:
    """Return the Starlette routes that implement the OAuth AS."""

    # ── /.well-known/oauth-authorization-server ──────────────────────
    async def authorization_server_metadata(_request: Request) -> JSONResponse:
        """RFC 8414 metadata. Critical: every field name matches the spec
        exactly — Claude (and reasonable clients) parse this verbatim and
        a misspelled key triggers a silent disconnect with no log line on
        the client side.
        """
        return JSONResponse(
            {
                "issuer": settings.issuer,
                "authorization_endpoint": f"{settings.issuer}/oauth/authorize",
                "token_endpoint": f"{settings.issuer}/oauth/token",
                "registration_endpoint": f"{settings.issuer}/oauth/register",
                "jwks_uri": f"{settings.issuer}/oauth/jwks.json",
                "response_types_supported": ["code"],
                "grant_types_supported": ["authorization_code"],
                "code_challenge_methods_supported": ["S256"],
                "token_endpoint_auth_methods_supported": [
                    "client_secret_basic",
                    "client_secret_post",
                    "none",
                ],
                "scopes_supported": ["openid", "email", "profile"],
                # RFC 8707 resource indicators — Claude sends ?resource=...
                # and we just echo back support.
                "resource_indicators_supported": True,
                # OAuth 2.1 marker (informational; spec doesn't define an
                # exact field but several clients sniff for it).
                "service_documentation": "https://github.com/carpenike/mcp",
            }
        )

    # ── /oauth/jwks.json ─────────────────────────────────────────────
    async def jwks(_request: Request) -> JSONResponse:
        return JSONResponse({"keys": [signing_key.public_jwk]})

    # ── /oauth/register (RFC 7591 DCR) ───────────────────────────────
    async def register(request: Request) -> JSONResponse:
        try:
            body = await request.json()
        except Exception:
            return _json_error(400, "invalid_client_metadata", "body is not valid JSON")

        if not isinstance(body, dict):
            return _json_error(400, "invalid_client_metadata", "body is not a JSON object")

        redirect_uris = body.get("redirect_uris")
        if not isinstance(redirect_uris, list) or not redirect_uris:
            return _json_error(
                400, "invalid_redirect_uri", "redirect_uris must be a non-empty array"
            )

        # All redirect URIs must match our allowlist. If even one is
        # disallowed we reject the whole registration — there's no good
        # reason a legitimate client would submit a mix.
        bad = [
            u for u in redirect_uris if not isinstance(u, str) or not _redirect_allowed(u, settings)
        ]
        if bad:
            return _json_error(
                400,
                "invalid_redirect_uri",
                f"redirect_uri not in allowlist: {bad[0]}",
            )

        client_name = body.get("client_name") or "unknown"
        token_endpoint_auth_method = body.get("token_endpoint_auth_method") or "client_secret_post"
        if token_endpoint_auth_method not in (
            "client_secret_basic",
            "client_secret_post",
            "none",
        ):
            return _json_error(
                400,
                "invalid_client_metadata",
                f"unsupported token_endpoint_auth_method: {token_endpoint_auth_method}",
            )

        client = await state.register_client(
            redirect_uris=redirect_uris,
            client_name=str(client_name),
            token_endpoint_auth_method=token_endpoint_auth_method,
        )

        log.info(
            "DCR: registered client %s (name=%r method=%s uris=%s)",
            client.client_id,
            client.client_name,
            client.token_endpoint_auth_method,
            client.redirect_uris,
        )

        response: dict[str, Any] = {
            "client_id": client.client_id,
            "client_id_issued_at": int(client.created_at),
            "redirect_uris": client.redirect_uris,
            "token_endpoint_auth_method": client.token_endpoint_auth_method,
            "grant_types": ["authorization_code"],
            "response_types": ["code"],
            "client_name": client.client_name,
        }
        if client.client_secret:
            response["client_secret"] = client.client_secret
            # 0 means "never expires" per RFC 7591 §3.2.1
            response["client_secret_expires_at"] = 0

        return JSONResponse(response, status_code=201)

    # ── /oauth/authorize ─────────────────────────────────────────────
    async def authorize(request: Request) -> Response:
        """Entry point for the interactive flow. Hand off to PocketID."""
        params = request.query_params

        client_id = params.get("client_id", "")
        redirect_uri = params.get("redirect_uri", "")
        response_type = params.get("response_type", "")
        code_challenge = params.get("code_challenge", "")
        code_challenge_method = params.get("code_challenge_method", "")
        claude_state = params.get("state")
        scope = params.get("scope")
        # resource = params.get("resource")  # informational; we always bind to our own resource_url

        # ── Validate ─────────────────────────────────────────────────
        if response_type != "code":
            return _json_error(
                400, "unsupported_response_type", "only response_type=code is supported"
            )

        client = await state.get_client(client_id)
        if client is None:
            return _json_error(400, "invalid_client", f"unknown client_id: {client_id}")

        if redirect_uri not in client.redirect_uris:
            return _json_error(
                400,
                "invalid_redirect_uri",
                "redirect_uri does not match a registered redirect_uri for this client",
            )

        if not code_challenge or code_challenge_method != "S256":
            return _json_error(
                400,
                "invalid_request",
                "code_challenge + code_challenge_method=S256 are required (PKCE)",
            )

        # ── Hand off to PocketID ─────────────────────────────────────
        upstream = await _load_upstream_metadata(settings)

        # Use a fresh state token for PocketID. The same token is the
        # key under which we stash the pending authorization, so the
        # callback can recover Claude's original context.
        state_token = secrets.token_urlsafe(32)
        nonce = secrets.token_urlsafe(16)
        # PocketID enforces PKCE on its OIDC endpoint, so we run a
        # second PKCE exchange between us and PocketID. The verifier
        # is stored in `pending` so the callback can present it on the
        # token exchange.
        upstream_verifier = secrets.token_urlsafe(64)
        upstream_challenge = _b64url_no_pad(
            hashlib.sha256(upstream_verifier.encode("ascii")).digest()
        )

        pending = PendingAuthorization(
            client_id=client_id,
            redirect_uri=redirect_uri,
            code_challenge=code_challenge,
            code_challenge_method=code_challenge_method,
            claude_state=claude_state,
            scope=scope,
            pocketid_code_verifier=upstream_verifier,
            pocketid_nonce=nonce,
            expires_at=time.time() + settings.oauth_code_lifetime_seconds,
        )
        await state.create_pending(state_token, pending)

        upstream_params = {
            "response_type": "code",
            "client_id": settings.pocketid_client_id,
            "redirect_uri": settings.pocketid_redirect_uri,
            "scope": "openid email profile",
            "state": state_token,
            "nonce": nonce,
            "code_challenge": upstream_challenge,
            "code_challenge_method": "S256",
        }
        location = upstream.authorization_endpoint + "?" + urlencode(upstream_params)
        log.info(
            "oauth.authorize: client=%s redirecting to PocketID (state=%s...)",
            client_id,
            state_token[:8],
        )
        return RedirectResponse(location, status_code=302)

    # ── /oauth/callback ──────────────────────────────────────────────
    async def callback(request: Request) -> Response:
        """PocketID redirects here after the user authenticates."""
        params = request.query_params

        if "error" in params:
            return _json_error(
                400,
                params.get("error", "upstream_error"),
                params.get("error_description") or "PocketID returned an error",
            )

        state_token = params.get("state", "")
        code = params.get("code", "")
        if not state_token or not code:
            return _json_error(400, "invalid_request", "missing code or state from upstream")

        pending = await state.pop_pending(state_token)
        if pending is None:
            return _json_error(400, "invalid_request", "unknown or expired state token")

        upstream = await _load_upstream_metadata(settings)

        # Exchange the PocketID code for tokens (server-to-server).
        async with httpx.AsyncClient(timeout=10) as client_http:
            token_resp = await client_http.post(
                upstream.token_endpoint,
                data={
                    "grant_type": "authorization_code",
                    "code": code,
                    "redirect_uri": settings.pocketid_redirect_uri,
                    "client_id": settings.pocketid_client_id,
                    "client_secret": settings.pocketid_client_secret,
                    # PocketID requires PKCE; the verifier matches the
                    # challenge we sent on /authorize.
                    "code_verifier": pending.pocketid_code_verifier,
                },
                headers={"Accept": "application/json"},
            )
        if token_resp.status_code != 200:
            log.warning(
                "pocketid token endpoint returned %s: %s",
                token_resp.status_code,
                token_resp.text[:300],
            )
            return _json_error(
                502, "upstream_error", "PocketID token endpoint refused the exchange"
            )

        tokens = token_resp.json()
        id_token = tokens.get("id_token")
        if not id_token:
            return _json_error(502, "upstream_error", "PocketID did not return an id_token")

        # Verify the id_token signature + claims.
        jwks_doc = await _load_upstream_jwks(upstream.jwks_uri)
        try:
            keys = JsonWebKey.import_key_set(jwks_doc)
            claims = JsonWebToken(["RS256", "ES256"]).decode(id_token, keys)
            claims.validate()  # exp/iat/nbf
        except Exception as e:
            log.warning("pocketid id_token rejected: %s", e)
            return _json_error(502, "upstream_error", f"invalid id_token from PocketID: {e}")

        if claims.get("iss") != upstream.issuer:
            return _json_error(502, "upstream_error", "id_token issuer mismatch")
        if claims.get("aud") != settings.pocketid_client_id:
            return _json_error(502, "upstream_error", "id_token audience mismatch")
        if claims.get("nonce") != pending.pocketid_nonce:
            return _json_error(502, "upstream_error", "id_token nonce mismatch")

        email = claims.get("email")
        if not email or not isinstance(email, str):
            return _json_error(
                403,
                "access_denied",
                "PocketID did not return an email claim",
            )

        if settings.oauth_user_allowlist and email not in settings.oauth_user_allowlist:
            return _json_error(403, "access_denied", f"user {email} not authorized")

        # Mint our own one-shot authorization code and redirect to Claude.
        our_code = secrets.token_urlsafe(32)
        await state.store_code(
            our_code,
            IssuedAuthorizationCode(
                client_id=pending.client_id,
                redirect_uri=pending.redirect_uri,
                code_challenge=pending.code_challenge,
                code_challenge_method=pending.code_challenge_method,
                user_email=email,
                scope=pending.scope,
                expires_at=time.time() + settings.oauth_code_lifetime_seconds,
            ),
        )

        return_params = {"code": our_code}
        if pending.claude_state is not None:
            return_params["state"] = pending.claude_state
        location = (
            pending.redirect_uri
            + ("&" if "?" in pending.redirect_uri else "?")
            + urlencode(return_params)
        )
        log.info("oauth.callback: minted code for user=%s client=%s", email, pending.client_id)
        return RedirectResponse(location, status_code=302)

    # ── /oauth/token ─────────────────────────────────────────────────
    async def token(request: Request) -> JSONResponse:
        """Exchange a code for a JWT access token."""
        form = await request.form()

        grant_type = str(form.get("grant_type", ""))
        if grant_type != "authorization_code":
            return _json_error(
                400, "unsupported_grant_type", "only authorization_code is supported"
            )

        code = str(form.get("code", ""))
        redirect_uri = str(form.get("redirect_uri", ""))
        code_verifier = str(form.get("code_verifier", ""))

        # ── Client authentication: HTTP Basic OR form params OR public/PKCE-only ──
        client_id, client_secret = _extract_client_credentials(request, form)
        if not client_id:
            return _json_error(401, "invalid_client", "client_id required")

        client = await state.get_client(client_id)
        if client is None:
            return _json_error(401, "invalid_client", f"unknown client_id: {client_id}")

        if client.token_endpoint_auth_method != "none" and not secrets.compare_digest(
            client_secret, client.client_secret
        ):
            return _json_error(401, "invalid_client", "client_secret mismatch")

        # ── Validate the code ────────────────────────────────────────
        issued = await state.consume_code(code)
        if issued is None:
            return _json_error(400, "invalid_grant", "code unknown or already used")
        if time.time() >= issued.expires_at:
            return _json_error(400, "invalid_grant", "code expired")
        if issued.client_id != client_id:
            return _json_error(400, "invalid_grant", "code was issued to a different client")
        if issued.redirect_uri != redirect_uri:
            return _json_error(400, "invalid_grant", "redirect_uri mismatch")
        if not _check_pkce(code_verifier, issued.code_challenge, issued.code_challenge_method):
            return _json_error(400, "invalid_grant", "PKCE verification failed")

        # ── Mint the JWT ─────────────────────────────────────────────
        now = int(time.time())
        access_token = (
            JsonWebToken(["RS256"])
            .encode(
                header={"alg": "RS256", "kid": signing_key.kid, "typ": "JWT"},
                payload={
                    "iss": settings.issuer,
                    "aud": settings.resource_url,
                    "sub": issued.user_email,
                    "email": issued.user_email,
                    "client_id": client_id,
                    "iat": now,
                    "nbf": now,
                    "exp": now + settings.oauth_access_token_lifetime_seconds,
                    "scope": issued.scope or "",
                },
                key=signing_key.private_pem,
            )
            .decode("ascii")
        )

        log.info(
            "oauth.token: issued JWT for user=%s client=%s expires_in=%d",
            issued.user_email,
            client_id,
            settings.oauth_access_token_lifetime_seconds,
        )
        return JSONResponse(
            {
                "access_token": access_token,
                "token_type": "Bearer",
                "expires_in": settings.oauth_access_token_lifetime_seconds,
                "scope": issued.scope or "",
            }
        )

    return [
        Route(
            "/.well-known/oauth-authorization-server",
            authorization_server_metadata,
            methods=["GET"],
        ),
        Route("/oauth/jwks.json", jwks, methods=["GET"]),
        Route("/oauth/register", register, methods=["POST"]),
        Route("/oauth/authorize", authorize, methods=["GET"]),
        Route("/oauth/callback", callback, methods=["GET"]),
        Route("/oauth/token", token, methods=["POST"]),
    ]


def _extract_client_credentials(request: Request, form: Any) -> tuple[str, str]:
    """Pull client_id+secret from either HTTP Basic or form params.

    Returns ('', '') if neither is supplied (the caller decides whether
    that's acceptable based on the client's registered auth method).
    """
    # RFC 6749 §2.3.1 — HTTP Basic preferred.
    auth = request.headers.get("authorization", "")
    if auth.lower().startswith("basic "):
        try:
            decoded = base64.b64decode(auth[6:]).decode("utf-8")
            cid, _, sec = decoded.partition(":")
            return cid, sec
        except Exception:
            return "", ""
    # Fall back to form body.
    cid = str(form.get("client_id", ""))
    sec = str(form.get("client_secret", ""))
    return cid, sec
