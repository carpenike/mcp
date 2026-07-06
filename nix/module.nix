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
#       };
#
#       # sops-managed file containing:
#       #   HOMELAB_MCP_POCKETID_CLIENT_SECRET=<from PocketID admin UI>
#       #   HOMELAB_MCP_GROCY_API_KEY=<from Grocy: Settings -> Manage API keys>
#       # and optionally:
#       #   HOMELAB_MCP_OAUTH_SIGNING_KEY=<RSA PEM, escaped newlines>
#       environmentFile = config.sops.secrets."homelab-mcp/env".path;
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

    networking.firewall.allowedTCPPorts = lib.mkIf cfg.openFirewall [ cfg.port ];
  };
}
