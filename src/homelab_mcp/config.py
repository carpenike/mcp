"""Settings for homelab-mcp.

All configuration comes from environment variables with the `HOMELAB_MCP_`
prefix. This mirrors the pattern used by the rest of the homelab services
(whiskey-whiskey-whiskey, replog, etc.) so consumption from NixOS
`systemd.services.<svc>.environment` + `EnvironmentFile` stays uniform.

Secrets (currently: the Cloudflare Access audience tag) should come via
`EnvironmentFile`; non-secret values can live in the NixOS module's
`settings = { ... }` block and show up in the Nix store.
"""

from __future__ import annotations

from typing import Any

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Runtime configuration loaded from `HOMELAB_MCP_*` env vars."""

    model_config = SettingsConfigDict(
        env_prefix="HOMELAB_MCP_",
        env_file=".env",  # for local dev only; production uses systemd EnvironmentFile
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # ── Server ───────────────────────────────────────────────────────
    bind_address: str = Field(
        default="127.0.0.1",
        description="Interface to bind. Keep on localhost — reverse proxy fronts this.",
    )
    port: int = Field(default=9200, ge=1, le=65535)
    log_level: str = Field(default="info", pattern="^(debug|info|warning|error|critical)$")

    # ── Cloudflare Access ────────────────────────────────────────────
    cf_access_required: bool = Field(
        default=True,
        description="If false, the JWT middleware is not installed. ONLY for local dev.",
    )
    cf_access_team: str = Field(
        default="",
        description=(
            "Cloudflare Zero Trust team subdomain — the bit before "
            "`.cloudflareaccess.com`. Example: 'bigheadltd'."
        ),
    )
    cf_access_app_id: str = Field(
        default="",
        description=(
            "OIDC Client ID of the Access for SaaS application — a 64-char "
            "hex string visible on the app's Setup page in the Cloudflare "
            "dashboard. This value is BOTH the OAuth client_id (used in "
            "the per-app issuer/JWKS URL paths) AND the expected `aud` "
            "claim of every access token. If your tokens carry a different "
            "`aud` for some reason, override with `cf_access_audience`."
        ),
    )
    cf_access_audience: str | None = Field(
        default=None,
        description=(
            "Override the expected `aud` claim. Defaults to `cf_access_app_id` "
            "which is correct for standard CF Access for SaaS deployments. "
            "Set this only if upstream changes the token shape."
        ),
    )
    public_base_url: str = Field(
        default="",
        description=(
            "The public URL clients use to reach this MCP server. Used to "
            "construct the `resource` claim in the RFC 9728 protected-resource "
            "metadata document. Example: 'https://mcp.holthome.net'. Required "
            "in production (CF Access enabled); optional in dev."
        ),
    )

    # ── Cooklang ─────────────────────────────────────────────────────
    cooklang_base_url: str = Field(
        default="https://cook.holthome.net",
        description="CookCLI web server (your personal recipe editor).",
    )
    federation_base_url: str = Field(
        default="https://fedcook.holthome.net",
        description="cooklang-federation server (search across your repo + community).",
    )
    recipes_dir: str = Field(
        default="/data/cooklang/recipes",
        description=(
            "Filesystem path to the cooklang recipes directory. "
            "`cooklang_save_recipe` writes under `<recipes_dir>/claude/`."
        ),
    )

    # ── Gatus ────────────────────────────────────────────────────────
    gatus_base_url: str = Field(
        default="https://gatus.holthome.net",
        description="Gatus uptime monitor base URL.",
    )

    # ── Derived ──────────────────────────────────────────────────────
    @property
    def cf_access_issuer(self) -> str:
        """The `iss` claim every CF Access (for SaaS) access token will carry.

        Per-app URL — Cloudflare scopes its OIDC issuer to each SaaS
        application rather than to the team as a whole.
        """
        return (
            f"https://{self.cf_access_team}.cloudflareaccess.com"
            f"/cdn-cgi/access/sso/oidc/{self.cf_access_app_id}"
        )

    @property
    def cf_access_jwks_url(self) -> str:
        """JWKS endpoint for the app's signing keys.

        Also per-app, not team-wide. Cloudflare rotates these keys
        periodically (so the JWKS cache TTL matters).
        """
        return f"{self.cf_access_issuer}/jwks"

    @property
    def cf_access_effective_audience(self) -> str:
        """The audience value the middleware actually validates against."""
        return self.cf_access_audience or self.cf_access_app_id

    # ── Cross-field validation ───────────────────────────────────────
    def model_post_init(self, __context: Any) -> None:
        """Fail loudly at startup if CF Access is required but not fully configured."""
        if self.cf_access_required:
            missing = [
                name
                for name, value in (
                    ("HOMELAB_MCP_CF_ACCESS_TEAM", self.cf_access_team),
                    ("HOMELAB_MCP_CF_ACCESS_APP_ID", self.cf_access_app_id),
                    ("HOMELAB_MCP_PUBLIC_BASE_URL", self.public_base_url),
                )
                if not value
            ]
            if missing:
                raise ValueError(
                    "Cloudflare Access JWT validation is required (default), but "
                    f"the following env vars are missing: {', '.join(missing)}. "
                    "Set them, or set HOMELAB_MCP_CF_ACCESS_REQUIRED=false (dev only)."
                )
