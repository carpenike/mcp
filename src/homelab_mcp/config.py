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
    trusted_proxy_ips: str = Field(
        default="127.0.0.1",
        description=(
            "Comma-separated peer IPs (or '*') whose X-Forwarded-* headers "
            "uvicorn will honor. This server sits behind a reverse proxy "
            "(Cloudflare Tunnel) that connects from loopback and sets the real "
            "client IP in X-Forwarded-For; the DCR rate limiter keys on that "
            "IP. Pinning this to the proxy's source address stops a direct-to-"
            "uvicorn attacker from spoofing X-Forwarded-For to mint fresh "
            "rate-limit buckets. Do NOT set to '*' on an internet-exposed bind."
        ),
    )

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
            "Wired into FastMCP's streamable_http_path at startup. The contract "
            "(pocketid-mcp-as v1.1) makes the MCP resource path app-declared; this "
            "server keeps its original '/mcp'. Used to build the spec-strict "
            "RFC 9728 §3.3 path-suffixed protected-resource metadata document VS "
            "Code requires (served at '/.well-known/oauth-protected-resource<mcp_path>')."
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
    oauth_refresh_token_lifetime_seconds: int = Field(
        default=30 * 86400,
        ge=86400,
        le=400 * 86400,
        description=(
            "Lifetime of issued refresh tokens. Refresh tokens are rotated on "
            "every use (the old one is invalidated and a new one returned), so "
            "this is the maximum idle window before a client must re-authenticate "
            "interactively. Without refresh tokens a client would have to re-run "
            "the full login flow every time the access token expires."
        ),
    )
    oauth_code_lifetime_seconds: int = Field(
        default=120,
        ge=30,
        le=600,
        description="Lifetime of authorization codes (one-shot, short-lived).",
    )

    oauth_state_db_path: str = Field(
        default="/var/lib/homelab-mcp/state.db",
        description=(
            "Path to the SQLite database that persists registered clients (DCR) "
            "and refresh tokens across restarts. Lives in the same StateDirectory "
            "as the signing key. Refresh tokens are stored as SHA-256 hashes, "
            "never in plaintext. Set to ':memory:' (or empty) to keep all OAuth "
            "state in-process — clients re-register and users re-authenticate on "
            "every restart. Short-lived state (in-flight PocketID round-trips and "
            "one-shot authorization codes) is always kept in memory regardless."
        ),
    )
    oauth_client_retention_seconds: int = Field(
        default=90 * 86400,
        ge=86400,
        description=(
            "How long a persisted DCR client with no live refresh token is kept "
            "before it's pruned. Because an in-use client always holds a "
            "non-expired refresh token, this only reaps abandoned registrations "
            "(e.g. from connector re-adds or reinstalls), bounding growth of the "
            "unauthenticated /oauth/register endpoint. Pruning runs at startup "
            "and opportunistically on each registration."
        ),
    )
    oauth_register_rate_limit_max: int = Field(
        default=30,
        ge=1,
        description=(
            "Maximum /oauth/register calls allowed per source IP within "
            "`oauth_register_rate_window_seconds`. A safety cap on the "
            "unauthenticated DCR endpoint; the single legitimate user registers "
            "rarely, so a modest limit never bites. Counters are in-memory "
            "(reset on restart)."
        ),
    )
    oauth_register_rate_window_seconds: int = Field(
        default=3600,
        ge=1,
        description="Sliding-window length for the /oauth/register rate limit.",
    )

    # Allowlist of redirect_uri prefixes accepted in DCR. Anything that
    # doesn't match one of these is dropped — defense against an attacker
    # registering a malicious redirect URI that turns our /authorize into
    # an open redirector.
    #
    # Matching is NOT a bare startswith(): `oauth_provider._redirect_allowed`
    # first parses the candidate URL and rejects any target carrying userinfo
    # (`user:pass@host`) or a malformed host, THEN applies the prefix check.
    # This is load-bearing — a naive startswith() on a prefix ending in ':'
    # (`http://localhost:`) is bypassable by `http://localhost:1@evil.com/`,
    # whose real host is evil.com. The upstream pocketid-mcp-as contract
    # originally specified these prefixes with prefix-match semantics and
    # inherited the same flaw; it was reported and fixed in v1.2.0 (parsed
    # scheme+host+port match + mandatory userinfo rejection), which this
    # implementation already satisfies.
    oauth_redirect_uri_allowlist: list[str] = Field(
        default=[
            # Claude (claude.ai web + Claude Desktop).
            "https://claude.ai/",
            "https://claude.com/",
            # VS Code 1.108+ sends four redirect_uris in a single DCR
            # request (see microsoft/vscode src/vs/base/common/oauth.ts
            # fetchDynamicRegistration). We accept all four shapes.
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
            "use time. Matching parses the URL and rejects embedded userinfo "
            "before the prefix check, so a ':'-terminated loopback prefix "
            "cannot be bypassed via 'http://localhost:1@evil.com/'."
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

    # ── DCR metadata size caps ───────────────────────────────────────
    # Bound the client metadata a single unauthenticated /oauth/register
    # call can persist, so a burst (even within the rate limit) can't
    # inflate the SQLite client table with oversized rows.
    oauth_dcr_max_redirect_uris: int = Field(
        default=8,
        ge=1,
        le=64,
        description="Max redirect_uris accepted in one DCR request.",
    )
    oauth_dcr_max_redirect_uri_length: int = Field(
        default=512,
        ge=16,
        description="Max length (chars) of any single redirect_uri in DCR.",
    )
    oauth_dcr_max_client_name_length: int = Field(
        default=256,
        ge=1,
        description="Max length (chars) of client_name accepted in DCR.",
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
        description=(
            "Base URL of the Grocy instance (no trailing slash needed). The "
            "Grocy REST API lives under '/api'; the tools append it "
            "automatically, so BOTH 'https://grocy.holthome.net' and "
            "'https://grocy.holthome.net/api' work."
        ),
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

    # ── Home Assistant ───────────────────────────────────────────────
    ha_base_url: str = Field(
        default="",
        description=(
            "Base URL of the Home Assistant instance, e.g. "
            "'https://hass.holthome.net' (no trailing slash needed; the REST "
            "API lives under '/api' and the tools append it). Empty disables "
            "the ha_* tools — they return a configuration error instead of "
            "calling out."
        ),
    )
    ha_token: str = Field(
        default="",
        description=(
            "Home Assistant long-lived access token, sent as "
            "'Authorization: Bearer' on every HA request. SECRET — supply via "
            "the sops-managed EnvironmentFile (HOMELAB_MCP_HA_TOKEN), never "
            "via the world-readable Nix `settings`. Create it under the HA "
            "user's profile → Security → Long-lived access tokens. Prefer a "
            "DEDICATED HA user; note the automation config-API tools "
            "(ha_get_automation / ha_upsert_automation) require that user to "
            "be an HA administrator. If empty, the ha_* tools return a "
            "configuration error instead of calling out."
        ),
    )
    ha_domain_allowlist: list[str] = Field(
        default=[
            "light",
            "switch",
            "fan",
            "scene",
            "script",
            "media_player",
            "climate",
            "vacuum",
            "humidifier",
            "input_boolean",
            "automation",
        ],
        description=(
            "Domains `ha_call_service` may actuate. BOTH the service domain "
            "and the target entity's domain must be on this list. Anything "
            "else is refused at the tool boundary — this is the HA "
            "equivalent of 'upstream URLs never come from user input': the "
            "callable service surface is operator-configured, not "
            "model-chosen. High-impact domains (lock, alarm_control_panel, "
            "cover, siren, valve) are deliberately absent from the default; "
            "adding one of them also subjects it to the confirm gate "
            "(`ha_confirm_domains`). Env value is a JSON array."
        ),
    )
    ha_confirm_domains: list[str] = Field(
        default=["lock", "alarm_control_panel", "cover", "siren", "valve"],
        description=(
            "Domains whose service calls additionally require confirm=true. "
            "Without it the tool returns a non-destructive preview of the "
            "target entity instead of actuating (same pattern as "
            "cooklang_delete_recipe). Only matters for domains that are ALSO "
            "on `ha_domain_allowlist`; the defaults keep the gate armed if "
            "an operator later allowlists a high-impact domain. Env value is "
            "a JSON array."
        ),
    )
    ha_confirm_timeout_seconds: float = Field(
        default=3.0,
        ge=0.1,
        le=30.0,
        description=(
            "How long `ha_call_service` polls the entity after a service "
            "call before reporting confirmed=false. HA acknowledges a "
            "service call when it is DISPATCHED, not when the device "
            "actually changed — Zigbee/Z-Wave/Wi-Fi round-trips take up to "
            "a few seconds. The tool re-reads the entity until it converges "
            "or this deadline passes, so 'confirmed' means observed state, "
            "never intent."
        ),
    )

    # ── ARC Raiders (public game-data upstreams, no secrets) ─────────
    arcraiders_metaforge_base_url: str = Field(
        default="https://metaforge.app/api/arc-raiders",
        description=(
            "Base URL of the MetaForge ARC Raiders REST API (items, quests, "
            "trader stock, event schedule). Free and keyless; MetaForge's "
            "docs require attribution and warn endpoints may change without "
            "notice, so responses carry a `source` field and live data is "
            "cached in-process."
        ),
    )
    arcraiders_data_base_url: str = Field(
        default="https://raw.githubusercontent.com/RaidTheory/arcraiders-data/main",
        description=(
            "Base URL for raw files of the RaidTheory/arcraiders-data GitHub "
            "repo (MIT-licensed community dataset behind arctracker.io). "
            "Pin a commit SHA instead of 'main' to freeze the dataset."
        ),
    )
    arcraiders_wiki_api_url: str = Field(
        default="https://arcraiders.wiki/w/api.php",
        description=(
            "MediaWiki Action API endpoint of the Embark-supported ARC "
            "Raiders wiki. Content is CC BY-SA 4.0."
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
            # The RFC 9728 §3.3 byte-match makes `public_base_url` load-bearing:
            # issuer/audience/resource are all derived from it, and a client
            # silently rejects a PRM whose `resource` doesn't equal the URL it
            # called. Catch obviously-broken values (missing scheme, trailing
            # slash, embedded path) at startup instead of shipping a PRM the
            # client will reject with no client-side log line.
            self._validate_public_base_url()

    def _validate_public_base_url(self) -> None:
        """Reject a malformed public_base_url before it poisons issuer/resource."""
        from urllib.parse import urlsplit

        raw = self.public_base_url
        parts = urlsplit(raw)
        problems: list[str] = []
        if parts.scheme not in ("http", "https"):
            problems.append("must start with http:// or https://")
        if not parts.hostname:
            problems.append("must include a host")
        # A bare trailing slash (path == "/") is tolerated and normalized away
        # by the derived issuer/resource properties; a real path segment is not.
        if parts.path not in ("", "/"):
            problems.append("must not include a path component")
        if parts.query or parts.fragment:
            problems.append("must not include a query or fragment")
        if parts.username is not None or parts.password is not None:
            problems.append("must not include userinfo")
        if problems:
            raise ValueError(
                "HOMELAB_MCP_PUBLIC_BASE_URL is malformed "
                f"({raw!r}): {'; '.join(problems)}. Expected exactly "
                "'<scheme>://<host>[:<port>]', e.g. 'https://mcp.holthome.net'."
            )
