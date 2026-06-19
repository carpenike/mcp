"""Settings for homelab-mcp.

All configuration comes from environment variables with the `HOMELAB_MCP_`
prefix. Mirrors the pattern used by the rest of the homelab services
(whiskey-whiskey-whiskey, cooklang, etc.).

Architecture summary
--------------------
homelab-mcp is its own OAuth 2.1 Authorization Server. It federates the
*user* login upstream to PocketID (an OIDC IdP), but Claude (and any
other MCP client) talks ONLY to homelab-mcp's own OAuth endpoints. We
mint our own JWTs signed with an RSA key local to the host, and validate
those JWTs in-process.

Why not let Claude talk to PocketID directly?
  PocketID doesn't implement RFC 7591 Dynamic Client Registration, which
  Claude requires.

Why not let Claude talk to Cloudflare Access directly?
  CF Access's OIDC discovery doc returns non-standard field names that
  Claude rejects (e.g. `grant_types_supported = ["authorization_code_with_pkce"]`
  instead of `["authorization_code"]`).

So we run our own spec-clean OAuth provider here, federate the human
login to PocketID, and issue Claude its own bearer tokens.
"""

from __future__ import annotations

from typing import Any

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Runtime configuration loaded from `HOMELAB_MCP_*` env vars."""

    model_config = SettingsConfigDict(
        env_prefix="HOMELAB_MCP_",
        env_file=".env",  # local dev only; production uses systemd EnvironmentFile
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # ── Server ───────────────────────────────────────────────────────
    bind_address: str = Field(default="127.0.0.1")
    port: int = Field(default=9200, ge=1, le=65535)
    log_level: str = Field(default="info", pattern="^(debug|info|warning|error|critical)$")

    # ── Public URL ───────────────────────────────────────────────────
    public_base_url: str = Field(
        default="",
        description=(
            "The public URL clients use to reach this MCP server (no trailing slash). "
            "Used as the OAuth `issuer` and the `audience` of every JWT we mint, and "
            "as the `resource` field in the RFC 9728 protected-resource metadata "
            "doc. Example: 'https://mcp.holthome.net'. REQUIRED in production."
        ),
    )
    mcp_path: str = Field(
        default="/mcp",
        description=(
            "Path component of the Streamable-HTTP MCP endpoint (leading slash). "
            "MUST match FastMCP's streamable_http_path (default '/mcp'). Used to "
            "build the spec-strict RFC 9728 §3.3 path-suffixed protected-resource "
            "metadata document VS Code requires (served at "
            "'/.well-known/oauth-protected-resource<mcp_path>')."
        ),
    )

    # ── OAuth provider ───────────────────────────────────────────────
    oauth_required: bool = Field(
        default=True,
        description=(
            "If false, the OAuth + JWT middleware are not installed. ONLY for "
            "local dev with no exposure beyond the loopback interface."
        ),
    )
    oauth_signing_key_path: str = Field(
        default="/var/lib/homelab-mcp/signing-key.pem",
        description=(
            "Path to the RSA private key (PEM, PKCS#8) used to sign access "
            "tokens. If the file does not exist at startup, a fresh 2048-bit "
            "RSA key is generated and written here (mode 0600). The matching "
            "public key is published at `/oauth/jwks.json` so any future "
            "verifier can validate offline. Set HOMELAB_MCP_OAUTH_SIGNING_KEY "
            "(the PEM contents directly) to bypass file loading; that env var "
            "is preferred for sops-managed keys."
        ),
    )
    oauth_signing_key: str | None = Field(
        default=None,
        description=(
            "RSA private key PEM contents (PKCS#8). Takes precedence over "
            "`oauth_signing_key_path`. Provide via a sops-managed env file "
            "so the key never touches disk. Multi-line values are fine; the "
            "EnvironmentFile parser handles backslash-escaped newlines."
        ),
    )
    oauth_access_token_lifetime_seconds: int = Field(
        default=86400, ge=60, le=7 * 86400, description="Lifetime of issued bearer tokens."
    )
    oauth_code_lifetime_seconds: int = Field(
        default=120,
        ge=30,
        le=600,
        description="Lifetime of authorization codes (one-shot, short-lived).",
    )

    # Allowlist of redirect_uri prefixes accepted in DCR. Anything that
    # doesn't start with one of these is rejected — defense against an
    # attacker registering a malicious redirect URI that turns our
    # /authorize into an open redirector.
    oauth_redirect_uri_allowlist: list[str] = Field(
        default=[
            # Claude (claude.ai web + Claude Desktop).
            "https://claude.ai/",
            "https://claude.com/",
            # VS Code 1.108+ sends four redirect_uris in a single DCR
            # request (see microsoft/vscode src/vs/base/common/oauth.ts
            # fetchDynamicRegistration). We accept all four shapes. Every
            # loopback prefix ends in ':' or '/' so a naive startswith()
            # can't be bypassed by 'http://127.0.0.1.evil.com/'.
            "https://vscode.dev/redirect",
            "https://insiders.vscode.dev/redirect",
            "http://127.0.0.1:",
            "http://127.0.0.1/",
            "http://localhost:",
            "http://localhost/",
        ],
        description=(
            "Allowlist of redirect_uri prefixes accepted in DCR + /authorize. "
            "DCR filters (not rejects) the submitted set against this list and "
            "stores only matches; /authorize then enforces the stored set at "
            "use time. Loopback prefixes MUST end in ':' or '/' to keep the "
            "startswith() check safe."
        ),
    )

    # Email allowlist for the `email` claim returned by PocketID. Only
    # users on this list are allowed to complete a login. If empty,
    # all PocketID users are allowed (single-user homelab assumption).
    oauth_user_allowlist: list[str] = Field(
        default=[],
        description=(
            "Allowlist of user emails permitted to sign in. Empty list = "
            "allow any PocketID-authenticated user. Set for multi-user setups."
        ),
    )

    # Used to sign the encrypted session cookie that holds in-flight
    # OAuth state across the redirect chain (Claude → us → PocketID → us).
    # If unset, a random 32-byte key is generated at startup; that means
    # in-flight authorizations are lost on restart, which is acceptable
    # because the cookie's TTL matches `oauth_code_lifetime_seconds`.
    oauth_session_secret: str | None = Field(
        default=None,
        description=(
            "Secret used to sign the session cookie. Optional; auto-generated "
            "at startup if unset. Surface via sops only if you want OAuth "
            "redirects to survive service restarts."
        ),
    )

    # ── Upstream IdP (PocketID) ──────────────────────────────────────
    pocketid_issuer: str = Field(
        default="",
        description=(
            "PocketID OIDC issuer URL, e.g. 'https://id.holthome.net'. Used "
            "for discovery: the /.well-known/openid-configuration is fetched "
            "from `<issuer>/.well-known/openid-configuration`."
        ),
    )
    pocketid_client_id: str = Field(
        default="",
        description=(
            "OIDC client ID registered for homelab-mcp in PocketID's admin UI. "
            "PocketID generates this when you create the client."
        ),
    )
    pocketid_client_secret: str = Field(
        default="",
        description=(
            "OIDC client secret for homelab-mcp's PocketID client. SOPS-managed; "
            "never appears in the Nix store."
        ),
    )
    # The redirect URI PocketID is configured to allow for our client.
    # Derived from public_base_url by default, but exposed as a setting
    # so a deploy can override (e.g. for staging environments).
    pocketid_redirect_path: str = Field(
        default="/oauth/callback",
        description="Path component of the redirect_uri Claude → us → PocketID → us flow.",
    )

    # ── Tool backends (unchanged from v0.1) ──────────────────────────
    cooklang_base_url: str = Field(default="https://cook.holthome.net")
    federation_base_url: str = Field(default="https://fedcook.holthome.net")
    recipes_dir: str = Field(default="/data/cooklang/recipes")
    gatus_base_url: str = Field(default="https://gatus.holthome.net")

    # ── Grocy ────────────────────────────────────────────────────────
    grocy_base_url: str = Field(
        default="https://grocy.holthome.net",
        description="Base URL of the Grocy instance (no trailing slash needed).",
    )
    grocy_api_key: str = Field(
        default="",
        description=(
            "API key sent in the `GROCY-API-KEY` header on every Grocy request. "
            "SECRET — supply via the sops-managed EnvironmentFile "
            "(HOMELAB_MCP_GROCY_API_KEY), never via the world-readable Nix "
            "`settings`. Generate one in Grocy under Settings → Manage API keys. "
            "If empty, the grocy_* tools return a configuration error instead of "
            "calling out."
        ),
    )

    # ── Derived ──────────────────────────────────────────────────────
    @property
    def pocketid_redirect_uri(self) -> str:
        """Absolute redirect URI registered in PocketID for our client."""
        return self.public_base_url.rstrip("/") + self.pocketid_redirect_path

    @property
    def issuer(self) -> str:
        """The `iss` claim we emit in every JWT we mint."""
        return self.public_base_url.rstrip("/")

    @property
    def resource_url(self) -> str:
        """The `resource` we advertise in the RFC 9728 metadata doc.

        Also used as the `aud` we validate on incoming JWTs. By making
        the issuer and the audience both equal to `public_base_url`,
        we ensure every token we mint is bound to THIS resource and
        cannot be replayed against a different MCP server.
        """
        return self.public_base_url.rstrip("/")

    @property
    def mcp_resource_url(self) -> str:
        """The canonical MCP endpoint URL (origin + mcp_path).

        This is the value spec-strict clients (VS Code) expect to see in
        the `resource` field of the path-suffixed RFC 9728 §3.3 PRM
        document, because it equals the exact URL they used to reach the
        MCP endpoint.
        """
        return self.public_base_url.rstrip("/") + "/" + self.mcp_path.strip("/")

    @property
    def prm_path_suffixed(self) -> str:
        """Path of the RFC 9728 §3.3 path-suffixed PRM endpoint.

        e.g. mcp_path='/mcp' -> '/.well-known/oauth-protected-resource/mcp'.
        VS Code 1.108+ fetches this exact path; serving only the origin-root
        variant makes VS Code reject the PRM and skip DCR entirely.
        """
        return "/.well-known/oauth-protected-resource/" + self.mcp_path.strip("/")

    # ── Cross-field validation ───────────────────────────────────────
    def model_post_init(self, __context: Any) -> None:
        """Fail loudly if OAuth is required but not fully configured."""
        if self.oauth_required:
            missing = [
                name
                for name, value in (
                    ("HOMELAB_MCP_PUBLIC_BASE_URL", self.public_base_url),
                    ("HOMELAB_MCP_POCKETID_ISSUER", self.pocketid_issuer),
                    ("HOMELAB_MCP_POCKETID_CLIENT_ID", self.pocketid_client_id),
                    ("HOMELAB_MCP_POCKETID_CLIENT_SECRET", self.pocketid_client_secret),
                )
                if not value
            ]
            if missing:
                raise ValueError(
                    "OAuth is required (default) but the following env vars "
                    f"are missing: {', '.join(missing)}. Set them, or set "
                    "HOMELAB_MCP_OAUTH_REQUIRED=false (dev only, no exposure)."
                )
