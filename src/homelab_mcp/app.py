"""homelab-mcp server entry point.

Composes:
  - the FastMCP server (tool transport)
  - the embedded OAuth 2.1 Authorization Server (oauth_provider)
  - the JWT auth middleware (auth.JWTAuthMiddleware)
  - the RFC 9728 protected-resource metadata route
  - uvicorn as the ASGI runner

The CLI install (`pyproject.toml -> [project.scripts]`) points
`homelab-mcp` at `main` here.
"""

from __future__ import annotations

import logging
import time
from typing import Any

import uvicorn
from mcp.server.fastmcp import FastMCP
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from homelab_mcp import __version__, oauth_provider, signing_key
from homelab_mcp import contract as contract_hosting
from homelab_mcp.auth import JWTAuthMiddleware
from homelab_mcp.buildinfo import build_revision
from homelab_mcp.config import Settings
from homelab_mcp.oauth_state import OAuthState
from homelab_mcp.tools import register_all

log = logging.getLogger("homelab_mcp")

HEALTH_PATH = "/healthz"


async def _healthz(_request: Request) -> JSONResponse:
    """Unauthenticated liveness probe.

    Cheap, dependency-free (does not touch upstreams or the DB) so a proxy /
    monitor can confirm the process is up without a bearer token. Surfaces
    the running version + build revision so a deploy can be verified.
    """
    return JSONResponse({"status": "ok", "version": __version__, "revision": build_revision()})


class RequestLogMiddleware:
    """Emit one INFO line per HTTP request: method, path, status, duration, user.

    Installed INNER of the JWT middleware so `scope["user"]` (set on a
    successful auth) is populated by the time we read it. Never logs headers,
    tokens, or bodies — only the metadata a homelab operator needs to see
    that the server is actually serving. This closes the "the observer is
    itself unobservable" gap without the noise of uvicorn's access log.
    """

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        start = time.monotonic()
        status_holder = {"status": 0}

        async def _send(message: Message) -> None:
            if message["type"] == "http.response.start":
                status_holder["status"] = message["status"]
            await send(message)

        try:
            await self.app(scope, receive, _send)
        finally:
            dur_ms = (time.monotonic() - start) * 1000.0
            user = scope.get("user") or {}
            email = user.get("email") if isinstance(user, dict) else None
            log.info(
                "%s %s -> %s (%.1fms)%s",
                scope.get("method", "?"),
                scope.get("path", "?"),
                status_holder["status"] or "-",
                dur_ms,
                f" user={email}" if email else "",
            )


def _build_protected_resource_metadata(settings: Settings, *, resource: str) -> dict[str, object]:
    """Construct an RFC 9728 protected-resource metadata document.

    Read by MCP clients to discover the authorization server. We point
    at ourselves because we ARE the AS.

    `resource` MUST equal the URL the client used to fetch the metadata's
    subject endpoint (RFC 9728 §3.3). claude.ai accepts the origin; VS
    Code 1.108+ requires the exact MCP endpoint URL via the path-suffixed
    variant. Callers pass the appropriate value per endpoint.
    """
    return {
        "resource": resource,
        "authorization_servers": [settings.issuer],
        "bearer_methods_supported": ["header"],
        "resource_signing_alg_values_supported": ["RS256"],
    }


def build_app(settings: Settings) -> Starlette:
    """Construct the Starlette ASGI app with the MCP transport + OAuth + JWT middleware."""
    # The string passed here is what MCP clients display to the user
    # (Claude shows it as the connector name on tool calls). Internal
    # naming stays "homelab-mcp" everywhere else (package, systemd
    # unit, user); this is purely the user-facing label.
    #
    # streamable_http_path pins the MCP transport at settings.mcp_path
    # (default '/mcp'). The pocketid-mcp-as v1.1 contract makes this path
    # app-declared; the RFC 9728 §3.3 path-suffixed PRM and its `resource`
    # byte-match are derived from the same setting, so the transport URL
    # and the advertised resource can never drift apart.
    mcp = FastMCP("Holthome", streamable_http_path=settings.mcp_path)

    # Load the signing key BEFORE tool registration so tools that need
    # to mint per-call JWTs for downstream resource calls (the HOF-004
    # tool-hop pattern) get a working minter. When OAuth is disabled
    # (local dev only), the minter is None and those tools log a warning
    # + skip registration.
    key: signing_key.SigningKey | None = None
    mint_token: Any = None
    if settings.oauth_required:
        key = signing_key.load_or_create(settings)

        # Closure capturing settings + key — keeps the tool modules from
        # importing oauth_provider directly.
        def mint_token(
            *,
            sub: str,
            email: str,
            audience: str,
            ttl_seconds: int = 60,
            client_id: str = "homelab-mcp-internal",
            scope: str = "",
        ) -> str:
            assert key is not None  # narrowed for mypy — see oauth_required guard above
            return oauth_provider.mint_tool_hop_token(
                settings,
                key,
                sub=sub,
                email=email,
                audience=audience,
                ttl_seconds=ttl_seconds,
                client_id=client_id,
                scope=scope,
            )

    register_all(mcp, settings, mint_token)

    # FastMCP exposes its Streamable HTTP transport as an ASGI app we can
    # mount middleware on. The route to call from clients is `settings.mcp_path`
    # (default `/mcp`).
    app: Starlette = mcp.streamable_http_app()

    # ── Host the pocketid-mcp-as contract publicly (Part B) ─────────
    # mcp.holthome.net is the contract's designated public home. These
    # routes are unauthenticated, GET-only, CORS-open and live entirely
    # outside the OAuth/bearer path — same posture as the .well-known
    # OAuth docs. Wired BEFORE the oauth_required early-return so they are
    # always served, then exempted from the JWT middleware below.
    for route in contract_hosting.build_routes():
        app.router.routes.append(route)

    # Unauthenticated liveness probe — always served, exempted from JWT below.
    app.router.routes.append(Route(HEALTH_PATH, _healthz, methods=["GET"]))

    if not settings.oauth_required:
        log.warning(
            "OAuth DISABLED — anyone who reaches this port can call any tool. "
            "Use for local dev ONLY."
        )
        return app

    assert key is not None  # narrowed: oauth_required is True here

    # ── Wire OAuth routes ───────────────────────────────────────────
    state = OAuthState.open(
        settings.oauth_state_db_path,
        client_retention_seconds=settings.oauth_client_retention_seconds,
        # A replayed refresh token only matters while its family could still
        # be live, so keep reuse-detection tombstones for the refresh TTL.
        consumed_retention_seconds=settings.oauth_refresh_token_lifetime_seconds,
    )
    # One-shot boot cleanup: drop expired refresh tokens and abandoned
    # DCR clients so the persisted table doesn't grow without bound.
    pruned = state.run_startup_maintenance()
    if pruned:
        log.info("OAuth startup maintenance: pruned %d abandoned client(s)", pruned)
    for route in oauth_provider.build_routes(settings, key, state):
        app.router.routes.append(route)

    # ── Wire RFC 9728 protected-resource metadata ───────────────────
    # Two variants per RFC 9728 §3.3:
    #   - origin-root  (/.well-known/oauth-protected-resource):
    #       resource = origin. claude.ai accepts this.
    #   - path-suffixed (/.well-known/oauth-protected-resource/<mcp_path>):
    #       resource = the exact MCP endpoint URL. VS Code 1.108+ requires
    #       this and rejects the PRM (skipping DCR) without it.
    prm_origin = _build_protected_resource_metadata(settings, resource=settings.resource_url)
    prm_mcp = _build_protected_resource_metadata(settings, resource=settings.mcp_resource_url)

    async def protected_resource_origin(_request: Request) -> JSONResponse:
        return JSONResponse(prm_origin)

    async def protected_resource_mcp(_request: Request) -> JSONResponse:
        return JSONResponse(prm_mcp)

    app.router.routes.append(
        Route(
            "/.well-known/oauth-protected-resource",
            protected_resource_origin,
            methods=["GET"],
        )
    )
    app.router.routes.append(
        Route(
            settings.prm_path_suffixed,
            protected_resource_mcp,
            methods=["GET"],
        )
    )

    # ── Install middleware ──────────────────────────────────────────
    # Order matters: add_middleware stacks last-added outermost. We want the
    # request logger INNER of the JWT layer so it can read scope["user"] that
    # JWT auth sets — so add the logger FIRST, then JWT auth.
    app.add_middleware(RequestLogMiddleware)
    app.add_middleware(
        JWTAuthMiddleware,
        signing_key=key,
        issuer=settings.issuer,
        audience=settings.resource_url,
        # RFC 9728 §5.3: the 401's WWW-Authenticate points spec-strict
        # clients (VS Code) at the path-suffixed PRM so they can discover
        # the AS without guessing well-known paths.
        resource_metadata_url=settings.issuer + settings.prm_path_suffixed,
        # The public contract-hosting docs + health probe stay outside the
        # bearer path.
        extra_unauthenticated_paths=contract_hosting.CONTRACT_PATHS | {HEALTH_PATH},
    )
    log.info(
        "OAuth enabled (issuer=%s audience=%s upstream=%s)",
        settings.issuer,
        settings.resource_url,
        settings.pocketid_issuer,
    )
    return app


def main() -> None:
    settings = Settings()
    logging.basicConfig(
        level=settings.log_level.upper(),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    log.info(
        "homelab-mcp starting on %s:%d (oauth=%s)",
        settings.bind_address,
        settings.port,
        "on" if settings.oauth_required else "OFF",
    )

    app = build_app(settings)

    # Trust X-Forwarded-* only from the configured proxy peer(s). Behind
    # Cloudflare Tunnel the real client IP arrives via X-Forwarded-For from
    # cloudflared on loopback; pinning forwarded_allow_ips stops a direct-to-
    # uvicorn attacker from spoofing it to defeat the DCR rate limiter.
    forwarded_allow_ips = [ip.strip() for ip in settings.trusted_proxy_ips.split(",") if ip.strip()]
    uvicorn.run(
        app,
        host=settings.bind_address,
        port=settings.port,
        log_level=settings.log_level,
        # Our RequestLogMiddleware emits the metadata we care about; uvicorn's
        # access log would just duplicate it more noisily.
        access_log=False,
        proxy_headers=True,
        forwarded_allow_ips=forwarded_allow_ips or "127.0.0.1",
    )


if __name__ == "__main__":
    main()
