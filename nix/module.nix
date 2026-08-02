# nix/module.nix
#
# NixOS module for homelab-mcp.
#
# Typical consumer wiring (e.g. carpenike/nix-config):
#
#   # flake.nix
#   inputs.homelab-mcp = {
#     url = "github:carpenike/mcp";
#     inputs.nixpkgs.follows = "nixpkgs";
#   };
#
#   # hosts/forge/services/homelab-mcp.nix
#   { config, inputs, pkgs, ... }: {
#     imports = [ inputs.homelab-mcp.nixosModules.default ];
#
#     services.homelab-mcp = {
#       enable = true;
#       package = inputs.homelab-mcp.packages.${pkgs.system}.default;
#
#       publicBaseUrl = "https://mcp.holthome.net";
#
#       settings = {
#         HOMELAB_MCP_POCKETID_ISSUER       = "https://id.holthome.net";
#         HOMELAB_MCP_POCKETID_CLIENT_ID    = "<from PocketID admin UI>";
#         HOMELAB_MCP_COOKLANG_BASE_URL     = "https://cook.holthome.net";
#         HOMELAB_MCP_FEDERATION_BASE_URL   = "https://fedcook.holthome.net";
#         HOMELAB_MCP_GATUS_BASE_URL        = "https://gatus.holthome.net";
#         HOMELAB_MCP_GROCY_BASE_URL        = "https://grocy.holthome.net";
#
#         # finances / paperless / messaging (non-secret halves)
#         HOMELAB_MCP_FINANCES_SIDECAR_BASE_URL = "http://127.0.0.1:9210";
#         # The monthly floor is a household decision; while unset,
#         # finances_monthly_summary reports gap_vs_floor as null rather
#         # than inventing a target.
#         # HOMELAB_MCP_FINANCES_FLOOR            = "8400";
#         # No mortgage setting: it is a synced off-budget account in Actual,
#         # so finances_debt_status reads it like every other balance.
#         HOMELAB_MCP_FINANCES_REPO_URL         = "https://github.com/carpenike/finances.git";
#         HOMELAB_MCP_PAPERLESS_BASE_URL        = "https://paperless.holthome.net";
#         HOMELAB_MCP_SIGNAL_BASE_URL           = "http://127.0.0.1:8484";
#         HOMELAB_MCP_SIGNAL_NUMBER             = "<E.164 registered number>";
#         HOMELAB_MCP_SIGNAL_GROUP_ID           = "group.<base64 id>";
#       };
#
#       # sops-managed file containing:
#       #   HOMELAB_MCP_POCKETID_CLIENT_SECRET=<from PocketID admin UI>
#       #   HOMELAB_MCP_GROCY_API_KEY=<from Grocy: Settings -> Manage API keys>
#       #   HOMELAB_MCP_FINANCES_SIDECAR_TOKEN=<shared with the sidecar>
#       #   HOMELAB_MCP_PAPERLESS_TOKEN=<dedicated paperless token>
#       # and optionally:
#       #   HOMELAB_MCP_OAUTH_SIGNING_KEY=<RSA PEM, escaped newlines>
#       environmentFile = config.sops.secrets."homelab-mcp/env".path;
#
#       # The finances_* tools need this; everything else works without it.
#       actualSidecar = {
#         enable = true;
#         serverUrl = "https://budget.holthome.net";
#         environmentFile = config.sops.secrets."homelab-mcp/actual-env".path;
#       };
#     };
#
#     # Reverse proxy + tunnel handled separately in your nix-config.
#   }

{ config, lib, pkgs, ... }:

let
  cfg = config.services.homelab-mcp;
in
{
  options.services.homelab-mcp = {
    enable = lib.mkEnableOption "homelab-mcp server (cooklang + gatus tools, embedded OAuth 2.1 AS)";

    package = lib.mkOption {
      type = lib.types.package;
      default = pkgs.python313Packages.callPackage ./package.nix { };
      defaultText = lib.literalExpression "pkgs.python313Packages.callPackage ./nix/package.nix { }";
      description = ''
        The homelab-mcp package to run. Consumers using the flake's
        overlay can leave this at default; otherwise set it to
        `inputs.homelab-mcp.packages.<system>.default`.
      '';
    };

    port = lib.mkOption {
      type = lib.types.port;
      default = 9200;
      description = ''
        TCP port to bind on `host`. Defaults to 9200 — 9100 is the
        well-known prometheus node-exporter port, which the homelab is
        very likely to be using.
      '';
    };

    host = lib.mkOption {
      type = lib.types.str;
      default = "127.0.0.1";
      description = ''
        Interface to bind. Default 127.0.0.1 — the standard topology is
        reverse proxy (Caddy) on the same host forwarding to localhost.
      '';
    };

    publicBaseUrl = lib.mkOption {
      type = lib.types.str;
      example = "https://mcp.holthome.net";
      description = ''
        Public URL clients use to reach this MCP server (no trailing slash).
        Used as the OAuth issuer + JWT audience, and as the `resource`
        in the RFC 9728 protected-resource metadata doc. Must match the
        URL Cloudflare Tunnel / Caddy exposes externally.
      '';
    };

    recipesDir = lib.mkOption {
      type = lib.types.path;
      default = "/data/cooklang/recipes";
      description = ''
        Cooklang recipes root, surfaced to the app as
        HOMELAB_MCP_RECIPES_DIR (settings.recipes_dir). The cooklang
        tools reach recipes over cook.holthome.net's HTTP API and never
        touch this path on disk — the app uses it only to compute
        recipe-relative paths. No filesystem permissions are granted
        for it, so it does not need to exist on the MCP host.
      '';
    };

    settings = lib.mkOption {
      type = with lib.types; attrsOf (oneOf [ str int bool ]);
      default = { };
      example = lib.literalExpression ''
        {
          HOMELAB_MCP_POCKETID_ISSUER     = "https://id.holthome.net";
          HOMELAB_MCP_POCKETID_CLIENT_ID  = "abcd1234";
          HOMELAB_MCP_COOKLANG_BASE_URL   = "https://cook.holthome.net";
          HOMELAB_MCP_FEDERATION_BASE_URL = "https://fedcook.holthome.net";
          HOMELAB_MCP_GATUS_BASE_URL      = "https://gatus.holthome.net";
          HOMELAB_MCP_GROCY_BASE_URL      = "https://grocy.holthome.net";
        }
      '';
      description = ''
        Declarative, NON-SECRET environment variables. Values appear in
        the Nix store world-readable — keep anything sensitive out.
        Use `environmentFile` for HOMELAB_MCP_POCKETID_CLIENT_SECRET
        and (optionally) HOMELAB_MCP_OAUTH_SIGNING_KEY.
      '';
    };

    environmentFile = lib.mkOption {
      type = lib.types.path;
      description = ''
        Path to an EnvironmentFile carrying secret config. Read by
        systemd before privileges drop, so it must be root-readable
        only (typical with sops-nix / agenix).

        Required keys:
          HOMELAB_MCP_POCKETID_CLIENT_SECRET=<from PocketID admin UI>

        Optional keys:
          HOMELAB_MCP_GROCY_API_KEY=<from Grocy: Settings -> Manage API keys>
            Required only if the grocy_* tools are used; without it those
            tools return a configuration error. Kept here (not in
            `settings`) because it is a secret.
          HOMELAB_MCP_FINANCES_SIDECAR_TOKEN=<shared secret>
            Must equal the sidecar's own SIDECAR_TOKEN. Defense in depth
            behind the loopback bind: without it any local process could
            read the household budget off the sidecar port.
          HOMELAB_MCP_FINANCES_REPO_TOKEN=<GitHub token for the finances repo>
            Reading the governance docs needs only `contents: read`, but
            finances_decision_append and finances_planned_append PUSH, so they
            require `contents: write`. With a read-only token the docs still
            serve and those two tools fail with an explicit permission error
            rather than silently. Passed to git via the environment, never in
            argv. A fine-grained PAT scoped to carpenike/finances alone is the
            least-privilege choice.
          HOMELAB_MCP_PAPERLESS_TOKEN=<paperless-ngx API token>
            Use a DEDICATED token for a least-privilege `homelab-mcp`
            service user (Django admin -> Tokens, or
            `manage.py drf_create_token homelab-mcp`). Do not reuse
            paperless-ai's or the admin's token — per-consumer tokens keep
            revocation surgical, and a superuser token bypasses paperless's
            object-level permissions entirely.

            Grant exactly: view_document, change_document, view_customfield,
            view_tag. NOT view_correspondent (correspondent filtering is a
            query parameter, not an endpoint lookup) and NOT add_customfield
            (the actual_txn / actual_account fields are declared once in
            paperless; this service reports a missing field as a config
            error rather than creating schema at runtime).

            Note: documents with an `owner` are invisible to a non-superuser
            unless shared, and that failure is silent — an empty result set,
            not an error.
          HOMELAB_MCP_OAUTH_SIGNING_KEY=<RSA private PEM, PKCS#8, escaped \n>
            If absent, the service generates and persists a fresh 2048-bit
            RSA key at /var/lib/homelab-mcp/signing-key.pem (mode 0600).
            Setting this via sops makes the key portable across hosts.
      '';
    };

    logLevel = lib.mkOption {
      type = lib.types.enum [ "debug" "info" "warning" "error" "critical" ];
      default = "info";
      description = "Python logging level for the homelab-mcp process.";
    };

    actualSidecar = {
      enable = lib.mkEnableOption ''
        the Actual Budget sidecar backing the finances_* tools.

        Actual has no HTTP query API and no API keys, so the Python tools
        cannot talk to it directly. This runs a small Node service on
        loopback that owns the `@actual-app/api` client, downloads the
        (end-to-end encrypted) budget once, and answers account/transaction
        queries. Without it the finances_* tools return a configuration
        error; every other tool category is unaffected
      '';

      package = lib.mkOption {
        type = lib.types.package;
        default = pkgs.callPackage ./sidecar.nix { };
        defaultText = lib.literalExpression "pkgs.callPackage ./nix/sidecar.nix { }";
        description = ''
          The sidecar package. Its `@actual-app/api` version is pinned in
          `sidecar/package.json`; that version must NEVER exceed the running
          Actual sync server's version, or the client migrates the budget
          file to a schema the server's web UI cannot read.
        '';
      };

      port = lib.mkOption {
        type = lib.types.port;
        default = 9210;
        description = "Loopback port the sidecar listens on.";
      };

      serverUrl = lib.mkOption {
        type = lib.types.str;
        example = "https://budget.holthome.net";
        description = "Base URL of the Actual sync server.";
      };

      syncTtlSeconds = lib.mkOption {
        type = lib.types.int;
        default = 300;
        description = ''
          How long a loaded budget is served before the next read re-syncs
          it from the Actual server. Bank data itself refreshes roughly
          daily, so this only bounds how stale the local copy can be
          relative to the server.
        '';
      };

      environmentFile = lib.mkOption {
        type = lib.types.path;
        description = ''
          sops-managed EnvironmentFile for the sidecar. Root-readable only.

          Required keys:
            ACTUAL_PASSWORD=<the budget.holthome.net login password>
            ACTUAL_BUDGET_SYNC_ID=<Settings -> Advanced -> Sync ID>
            SIDECAR_TOKEN=<shared secret; must equal
                           HOMELAB_MCP_FINANCES_SIDECAR_TOKEN>

          Required when the budget file is end-to-end encrypted:
            ACTUAL_ENCRYPTION_PASSWORD=<the file encryption password>

          NOTE: Actual rate-limits /account/login at 5 FAILED attempts per
          15 minutes. A wrong password here does not retry in-process — the
          service exits and systemd backs off — but repeated restarts with
          bad credentials will still lock the account out. Verify the values
          before deploying.
        '';
      };
    };

    openFirewall = lib.mkOption {
      type = lib.types.bool;
      default = false;
      description = ''
        Open `port` in the host firewall. Default off — the reverse
        proxy on the same box forwards to localhost.
      '';
    };
  };

  config = lib.mkIf cfg.enable {
    # Dedicated system user, declared explicitly (rather than DynamicUser)
    # so the StateDirectory at /var/lib/homelab-mcp — which persists the
    # auto-generated OAuth signing key and the client/token SQLite store —
    # keeps stable ownership across restarts and package upgrades.
    users.users.homelab-mcp = {
      isSystemUser = true;
      group = "homelab-mcp";
      description = "homelab-mcp service user";
      home = "/var/lib/homelab-mcp";
      createHome = false; # StateDirectory handles it.
    };
    users.groups.homelab-mcp = { };

    systemd.services.homelab-mcp = {
      description = "homelab-mcp server (Model Context Protocol)";
      wantedBy = [ "multi-user.target" ];
      after = [ "network.target" ];

      environment = {
        HOMELAB_MCP_BIND_ADDRESS = cfg.host;
        HOMELAB_MCP_PORT = toString cfg.port;
        HOMELAB_MCP_LOG_LEVEL = cfg.logLevel;
        HOMELAB_MCP_RECIPES_DIR = toString cfg.recipesDir;
        HOMELAB_MCP_PUBLIC_BASE_URL = cfg.publicBaseUrl;
        # The OAuth signing key persists here when not supplied via env.
        # The PEM file is created mode 0600 on first start.
        HOMELAB_MCP_OAUTH_SIGNING_KEY_PATH = "/var/lib/homelab-mcp/signing-key.pem";
        # SQLite store for registered clients (DCR) + refresh tokens, so
        # Claude survives a service restart without re-authenticating.
        # Created on first start; WAL mode adds -wal/-shm sidecars. Lives
        # in the same StateDirectory (implicitly in ReadWritePaths).
        HOMELAB_MCP_OAUTH_STATE_DB_PATH = "/var/lib/homelab-mcp/state.db";
        # Private clone of the finances governance repo, kept lazily fresh.
        # Inside the 0700 StateDirectory: it holds household financial planning.
        HOMELAB_MCP_FINANCES_REPO_PATH = "/var/lib/homelab-mcp/finances";
      } // lib.mapAttrs (_n: v: toString v) cfg.settings;

      serviceConfig = {
        ExecStart = lib.getExe cfg.package;
        EnvironmentFile = cfg.environmentFile;
        Restart = "on-failure";
        RestartSec = "5s";

        User = "homelab-mcp";
        Group = "homelab-mcp";

        # Owned-by-systemd state directory at /var/lib/homelab-mcp,
        # mode 0700, used to persist the auto-generated RSA signing key
        # across restarts.
        StateDirectory = "homelab-mcp";
        StateDirectoryMode = "0700";

        # Hardening — same shape as the cooklang module's main service.
        ProtectSystem = "strict";
        ProtectHome = true;
        PrivateTmp = true;
        PrivateDevices = true;
        ProtectKernelTunables = true;
        ProtectKernelModules = true;
        ProtectControlGroups = true;
        NoNewPrivileges = true;
        RestrictRealtime = true;
        RestrictSUIDSGID = true;
        LockPersonality = true;
        # Python's interpreter doesn't need WX (no JIT). Tighten this.
        MemoryDenyWriteExecute = true;
        SystemCallFilter = [ "@system-service" "~@privileged" "~@resources" ];
        CapabilityBoundingSet = [ "" ];

        # The only writable location the service needs is its
        # StateDirectory (/var/lib/homelab-mcp), which systemd adds to
        # ReadWritePaths implicitly. Everything else stays read-only via
        # ProtectSystem=strict — the cooklang tools write recipes over
        # cook.holthome.net's HTTP API, not the local filesystem.

        RestrictAddressFamilies = [ "AF_INET" "AF_INET6" "AF_UNIX" ];
        MemoryMax = "256M";
        CPUQuota = "50%";
        TasksMax = "32";
      };
    };

    # ── Actual sidecar ────────────────────────────────────────────────
    systemd.services.homelab-mcp-actual-sidecar = lib.mkIf cfg.actualSidecar.enable {
      description = "Actual Budget sidecar for homelab-mcp finances tools";
      wantedBy = [ "multi-user.target" ];
      after = [ "network-online.target" ];
      wants = [ "network-online.target" ];
      # homelab-mcp itself starts fine without this (finances_* just report a
      # configuration error), so this is a soft ordering, not a Requires=.
      before = [ "homelab-mcp.service" ];

      environment = {
        SIDECAR_HOST = "127.0.0.1";
        SIDECAR_PORT = toString cfg.actualSidecar.port;
        SIDECAR_SYNC_TTL_SECONDS = toString cfg.actualSidecar.syncTtlSeconds;
        ACTUAL_SERVER_URL = cfg.actualSidecar.serverUrl;
        # Holds the decrypted budget copy. Inside the service's own
        # StateDirectory, which is 0700 and owned by the service user.
        ACTUAL_DATA_DIR = "/var/lib/homelab-mcp-actual/budget";
      };

      serviceConfig = {
        ExecStart = lib.getExe cfg.actualSidecar.package;
        EnvironmentFile = cfg.actualSidecar.environmentFile;

        # Deliberately slow restarts. A crash loop here would hammer
        # /account/login, which locks out after 5 failures in 15 minutes.
        Restart = "on-failure";
        RestartSec = "60s";
        StartLimitBurst = 3;
        StartLimitIntervalSec = 900;

        User = "homelab-mcp";
        Group = "homelab-mcp";

        StateDirectory = "homelab-mcp-actual";
        StateDirectoryMode = "0700";

        # Same hardening shape as the main service, minus
        # MemoryDenyWriteExecute: V8 is a JIT and needs W+X pages.
        ProtectSystem = "strict";
        ProtectHome = true;
        PrivateTmp = true;
        PrivateDevices = true;
        ProtectKernelTunables = true;
        ProtectKernelModules = true;
        ProtectControlGroups = true;
        NoNewPrivileges = true;
        RestrictRealtime = true;
        RestrictSUIDSGID = true;
        LockPersonality = true;
        SystemCallFilter = [ "@system-service" "~@privileged" "~@resources" ];
        CapabilityBoundingSet = [ "" ];
        RestrictAddressFamilies = [ "AF_INET" "AF_INET6" "AF_UNIX" ];
        # The budget file plus better-sqlite3 need real headroom; 256M (the
        # Python service's limit) OOM-kills this during the initial download.
        MemoryMax = "1G";
        CPUQuota = "75%";
        TasksMax = "32";
      };
    };

    networking.firewall.allowedTCPPorts = lib.mkIf cfg.openFirewall [ cfg.port ];
  };
}
