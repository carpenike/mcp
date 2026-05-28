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
from typing import Any

import uvicorn
from mcp.server.fastmcp import FastMCP
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

from homelab_mcp import oauth_provider, signing_key
from homelab_mcp.auth import JWTAuthMiddleware
from homelab_mcp.config import Settings
from homelab_mcp.oauth_state import OAuthState
from homelab_mcp.tools import register_all

log = logging.getLogger("homelab_mcp")


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
    mcp = FastMCP("Holthome")

    # Load the signing key BEFORE tool registration so tools that need
    # to mint per-call JWTs for downstream resource calls (e.g. the
    # replog.py module per HOF-004) get a working minter. When OAuth is
    # disabled (local dev only), the minter is None and those tools
    # log a warning + skip registration.
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
    # mount middleware on. The route to call from clients is `/mcp`.
    app: Starlette = mcp.streamable_http_app()

    if not settings.oauth_required:
        log.warning(
            "OAuth DISABLED — anyone who reaches this port can call any tool. "
            "Use for local dev ONLY."
        )
        return app

    assert key is not None  # narrowed: oauth_required is True here

    # ── Wire OAuth routes ───────────────────────────────────────────
    state = OAuthState()
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

    # ── Install JWT middleware ──────────────────────────────────────
    app.add_middleware(
        JWTAuthMiddleware,
        signing_key=key,
        issuer=settings.issuer,
        audience=settings.resource_url,
        # RFC 9728 §5.3: the 401's WWW-Authenticate points spec-strict
        # clients (VS Code) at the path-suffixed PRM so they can discover
        # the AS without guessing well-known paths.
        resource_metadata_url=settings.issuer + settings.prm_path_suffixed,
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

    uvicorn.run(
        app,
        host=settings.bind_address,
        port=settings.port,
        log_level=settings.log_level,
        # Uvicorn's access log spams DEBUG-level lines per request and the
        # claim metadata we want is already logged by JWTAuthMiddleware.
        access_log=False,
    )


if __name__ == "__main__":
    main()
