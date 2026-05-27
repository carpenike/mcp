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

from homelab_mcp.auth import JWKSCache, JWTAuthMiddleware
from homelab_mcp.config import Settings
from homelab_mcp.tools import register_all

log = logging.getLogger("homelab_mcp")


def build_app(settings: Settings) -> Starlette:
    """Construct the Starlette ASGI app with the MCP transport and JWT middleware."""
    mcp = FastMCP("homelab-mcp")
    register_all(mcp, settings)

    # FastMCP exposes its Streamable HTTP transport as an ASGI app we can
    # mount middleware on. The route to call from clients is `/mcp`.
    app: Starlette = mcp.streamable_http_app()

    if settings.cf_access_required:
        jwks = JWKSCache(settings.cf_access_jwks_url)
        app.add_middleware(
            JWTAuthMiddleware,
            jwks_cache=jwks,
            issuer=settings.cf_access_issuer,
            audience=settings.cf_access_aud,
        )
        log.info(
            "CF Access JWT validation enabled (iss=%s aud=%s)",
            settings.cf_access_issuer,
            settings.cf_access_aud,
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
