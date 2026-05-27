"""homelab-mcp server entry point.

Composes the MCP server with the JWT auth middleware and starts uvicorn.

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

from homelab_mcp.auth import JWKSCache, JWTAuthMiddleware
from homelab_mcp.config import Settings
from homelab_mcp.tools import register_all

log = logging.getLogger("homelab_mcp")


def _build_protected_resource_metadata(settings: Settings) -> dict[str, object]:
    """Construct the RFC 9728 protected-resource metadata document.

    This is what Claude (and any MCP client) reads from
    `/.well-known/oauth-protected-resource` to discover the authorization
    server it needs to talk to. Without this, Claude's MCP custom-connector
    flow falls back to assuming the AS is co-located with the MCP server at
    `<base>/authorize` and `<base>/token`, which is wrong for our deployment
    (the AS lives at Cloudflare Access, not on our origin).
    """
    return {
        "resource": settings.public_base_url,
        "authorization_servers": [settings.cf_access_issuer],
        # Bearer-token presentation methods accepted by our middleware.
        "bearer_methods_supported": ["header"],
        # Informational — tells the client our middleware uses RS256.
        "resource_signing_alg_values_supported": ["RS256"],
    }


def build_app(settings: Settings) -> Starlette:
    """Construct the Starlette ASGI app with the MCP transport and JWT middleware."""
    mcp = FastMCP("homelab-mcp")
    register_all(mcp, settings)

    # FastMCP exposes its Streamable HTTP transport as an ASGI app we can
    # mount middleware on. The route to call from clients is `/mcp`.
    app: Starlette = mcp.streamable_http_app()

    # Add discovery routes. These must be reachable WITHOUT auth so OAuth
    # clients can find the authorization server before they have a token.
    # The JWTAuthMiddleware allowlists their paths (see auth.py).
    if settings.cf_access_required:
        prm = _build_protected_resource_metadata(settings)

        async def oauth_protected_resource(_request: Request) -> JSONResponse:
            return JSONResponse(prm)

        app.router.routes.append(
            Route(
                "/.well-known/oauth-protected-resource",
                oauth_protected_resource,
                methods=["GET"],
            )
        )

    if settings.cf_access_required:
        jwks = JWKSCache(settings.cf_access_jwks_url)
        app.add_middleware(
            JWTAuthMiddleware,
            jwks_cache=jwks,
            issuer=settings.cf_access_issuer,
            audience=settings.cf_access_effective_audience,
        )
        log.info(
            "CF Access JWT validation enabled (iss=%s aud=%s)",
            settings.cf_access_issuer,
            settings.cf_access_effective_audience,
        )
        log.info(
            "Protected-resource metadata served at /.well-known/oauth-protected-resource"
            " (resource=%s)",
            settings.public_base_url,
        )
    else:
        log.warning(
            "CF Access JWT validation DISABLED — anyone who reaches this port "
            "can call any tool. Use for local dev ONLY."
        )

    return app


def main() -> None:
    settings = Settings()
    logging.basicConfig(
        level=settings.log_level.upper(),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    log.info(
        "homelab-mcp starting on %s:%d (cf_access=%s)",
        settings.bind_address,
        settings.port,
        "on" if settings.cf_access_required else "OFF",
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
