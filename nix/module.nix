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
#       settings = {
#         HOMELAB_MCP_CF_ACCESS_TEAM       = "bigheadltd";
#         HOMELAB_MCP_COOKLANG_BASE_URL    = "https://cook.holthome.net";
#         HOMELAB_MCP_FEDERATION_BASE_URL  = "https://fedcook.holthome.net";
#         HOMELAB_MCP_GATUS_BASE_URL       = "https://gatus.holthome.net";
#       };
#
#       # File contents (sops-managed):
#       #   HOMELAB_MCP_CF_ACCESS_APP_ID=<OIDC Client ID from CF Access SaaS app>
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
    enable = lib.mkEnableOption "homelab-mcp server (cooklang + gatus tools)";

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
      default = 9100;
      description = "TCP port to bind on `host`.";
    };

    host = lib.mkOption {
      type = lib.types.str;
      default = "127.0.0.1";
      description = ''
        Interface to bind. Default 127.0.0.1 — the standard topology is
        reverse proxy (Caddy) on the same host forwarding to localhost.
      '';
    };

    recipesDir = lib.mkOption {
      type = lib.types.path;
      default = "/data/cooklang/recipes";
      description = ''
        Filesystem path to the cooklang recipes directory. The MCP
        service runs with ReadWritePaths covering this path so the
        `cooklang_save_recipe` tool can create files. We also create
        `<recipesDir>/claude/` at startup with mode 02770 owned
        `cooklang:<recipesGroup>` so the MCP user (which has
        recipesGroup as a supplementary group) can write into it.
      '';
    };

    recipesGroup = lib.mkOption {
      type = lib.types.str;
      default = "cooklang";
      description = ''
        Group the MCP service runs under as a supplementary group, so
        it can write into `<recipesDir>/claude/` (which is owned by
        the cooklang service's user/group on forge).
      '';
    };

    settings = lib.mkOption {
      type = with lib.types; attrsOf (oneOf [ str int bool ]);
      default = { };
      example = lib.literalExpression ''
        {
          HOMELAB_MCP_CF_ACCESS_TEAM      = "bigheadltd";
          HOMELAB_MCP_COOKLANG_BASE_URL   = "https://cook.holthome.net";
          HOMELAB_MCP_FEDERATION_BASE_URL = "https://fedcook.holthome.net";
          HOMELAB_MCP_GATUS_BASE_URL      = "https://gatus.holthome.net";
        }
      '';
      description = ''
        Declarative, NON-SECRET environment variables. Values appear in
        the Nix store world-readable — keep anything sensitive out.
        Use `environmentFile` for HOMELAB_MCP_CF_ACCESS_APP_ID and any
        future secret values.
      '';
    };

    environmentFile = lib.mkOption {
      type = lib.types.path;
      description = ''
        Path to an EnvironmentFile carrying secret config. Read by
        systemd before privileges drop, so it must be root-readable
        only (typical with sops-nix / agenix).

        Required keys:
          HOMELAB_MCP_CF_ACCESS_APP_ID=<OIDC Client ID from the CF Access SaaS app>
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
    # Dedicated system user. DynamicUser doesn't compose well with
    # supplementary group membership (we need access to the cooklang
    # group to write under /data/cooklang/recipes), so we declare a
    # real user.
    users.users.homelab-mcp = {
      isSystemUser = true;
      group = "homelab-mcp";
      extraGroups = [ cfg.recipesGroup ];
      description = "homelab-mcp service user";
    };
    users.groups.homelab-mcp = { };

    # Pre-create the `claude/` subdirectory that `cooklang_save_recipe`
    # writes into. Mode 02770 = group-writable + setgid sticky, so
    # files created here inherit `cooklang` as their group even though
    # the homelab-mcp user creates them. This keeps the cooklang
    # systemd service (which reads them) happy.
    systemd.tmpfiles.rules = [
      "d ${cfg.recipesDir}/claude 2770 cooklang ${cfg.recipesGroup} -"
    ];

    systemd.services.homelab-mcp = {
      description = "homelab-mcp server (Model Context Protocol)";
      wantedBy = [ "multi-user.target" ];
      after = [ "network.target" ];
      # Don't start until the recipes ZFS dataset is mounted; otherwise
      # save_recipe would write into a tmpfs the cooklang service won't see.
      unitConfig.RequiresMountsFor = [ cfg.recipesDir ];

      environment = {
        HOMELAB_MCP_BIND_ADDRESS = cfg.host;
        HOMELAB_MCP_PORT = toString cfg.port;
        HOMELAB_MCP_LOG_LEVEL = cfg.logLevel;
        HOMELAB_MCP_RECIPES_DIR = toString cfg.recipesDir;
      } // lib.mapAttrs (_n: v: toString v) cfg.settings;

      serviceConfig = {
        ExecStart = lib.getExe cfg.package;
        EnvironmentFile = cfg.environmentFile;
        Restart = "on-failure";
        RestartSec = "5s";

        User = "homelab-mcp";
        Group = "homelab-mcp";

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

        # MCP server writes recipes under recipesDir; everything else is
        # read-only via ProtectSystem=strict.
        ReadWritePaths = [ cfg.recipesDir ];

        RestrictAddressFamilies = [ "AF_INET" "AF_INET6" "AF_UNIX" ];
        MemoryMax = "256M";
        CPUQuota = "50%";
        TasksMax = "32";
      };
    };

    networking.firewall.allowedTCPPorts = lib.mkIf cfg.openFirewall [ cfg.port ];
  };
}
