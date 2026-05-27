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


def _build_protected_resource_metadata(settings: Settings) -> dict[str, object]:
    """Construct the RFC 9728 protected-resource metadata document.

    Read by Claude (and any compliant MCP client) from
    `/.well-known/oauth-protected-resource` to discover the authorization
    server. We point at ourselves because we ARE the AS.
    """
    return {
        "resource": settings.resource_url,
        "authorization_servers": [settings.issuer],
        "bearer_methods_supported": ["header"],
        "resource_signing_alg_values_supported": ["RS256"],
    }


def build_app(settings: Settings) -> Starlette:
    """Construct the Starlette ASGI app with the MCP transport + OAuth + JWT middleware."""
    mcp = FastMCP("homelab-mcp")
    register_all(mcp, settings)

    # FastMCP exposes its Streamable HTTP transport as an ASGI app we can
    # mount middleware on. The route to call from clients is `/mcp`.
    app: Starlette = mcp.streamable_http_app()

    if not settings.oauth_required:
        log.warning(
            "OAuth DISABLED — anyone who reaches this port can call any tool. "
            "Use for local dev ONLY."
        )
        return app

    # ── Load (or generate) the RSA signing key ──────────────────────
    key = signing_key.load_or_create(settings)

    # ── Wire OAuth routes ───────────────────────────────────────────
    state = OAuthState()
    for route in oauth_provider.build_routes(settings, key, state):
        app.router.routes.append(route)

    # ── Wire RFC 9728 protected-resource metadata ───────────────────
    prm = _build_protected_resource_metadata(settings)

    async def protected_resource(_request: Request) -> JSONResponse:
        return JSONResponse(prm)

    app.router.routes.append(
        Route(
            "/.well-known/oauth-protected-resource",
            protected_resource,
            methods=["GET"],
        )
    )

    # ── Install JWT middleware ──────────────────────────────────────
    app.add_middleware(
        JWTAuthMiddleware,
        signing_key=key,
        issuer=settings.issuer,
        audience=settings.resource_url,
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
