{
  description = "homelab-mcp — cooklang + gatus tools exposed via Model Context Protocol";

  inputs = {
    # Pinned to nixos-25.11 to match the consumer channel (carpenike/nix-config).
    # Override with:
    #   inputs.homelab-mcp.inputs.nixpkgs.follows = "nixpkgs";
    # in the consumer flake to share a single nixpkgs across the closure.
    nixpkgs.url = "github:NixOS/nixpkgs/nixos-25.11";
  };

  outputs = { self, nixpkgs }:
    let
      # Deployment target is x86_64-linux (forge). The aarch64 / darwin
      # entries exist so dev machines (rymac M-series, rydev/nixpi) can
      # `nix run` for smoke testing.
      supportedSystems = [
        "x86_64-linux"
        "aarch64-linux"
        "aarch64-darwin"
        "x86_64-darwin"
      ];
      forAllSystems = f:
        nixpkgs.lib.genAttrs supportedSystems (system: f {
          inherit system;
          pkgs = import nixpkgs { inherit system; };
        });

      mkPackage = pkgs: pkgs.python313Packages.callPackage ./nix/package.nix {
        # Bake the flake revision into the build so `homelab-mcp --version`
        # (when we add one) reports the deployed commit. Falls back to
        # `dirtyRev` for uncommitted source trees, then "unknown".
        gitRev = self.rev or self.dirtyRev or "unknown";
        buildTime = self.lastModifiedDate or "unknown";
      };
    in
    {
      packages = forAllSystems ({ pkgs, ... }: {
        default = mkPackage pkgs;
        homelab-mcp = mkPackage pkgs;
      });

      # `nix run github:carpenike/mcp`
      apps = forAllSystems ({ system, ... }: {
        default = {
          type = "app";
          program = "${self.packages.${system}.default}/bin/homelab-mcp";
        };
      });

      # `nix flake check` builds the package on every supported system. The
      # Python test suite is run separately by CI (faster feedback loop than
      # rebuilding the derivation).
      checks = forAllSystems ({ system, ... }: {
        package = self.packages.${system}.default;
      });

      devShells = forAllSystems ({ pkgs, ... }: {
        default = pkgs.mkShell {
          packages = [
            (pkgs.python313.withPackages (ps: with ps; [
              # Runtime deps. SOURCE OF TRUTH is pyproject.toml
              # [project.dependencies]; this list is a hand-maintained mirror
              # for the dev shell and can drift. When you change a dependency
              # in pyproject.toml, update this list (and nix/package.nix's
              # `dependencies`) to match — there is no automatic derivation.
              mcp
              starlette
              uvicorn
              httpx
              authlib
              pyjwt
              cryptography
              pydantic
              pydantic-settings
              # Dev tools.
              pip
              pytest
              pytest-asyncio
              pytest-httpx
              mypy
              ruff
            ]))
            pkgs.uv
          ];
          shellHook = ''
            echo "homelab-mcp devshell ($(python --version 2>&1))"
            echo "  pytest                 — run tests"
            echo "  ruff check . && ruff format --check ."
            echo "  mypy src               — type check"
            echo "  homelab-mcp            — run the server (set HOMELAB_MCP_OAUTH_REQUIRED=false for local dev)"
          '';
        };
      });

      # NixOS module — import via:
      #   imports = [ inputs.homelab-mcp.nixosModules.default ];
      nixosModules.default = import ./nix/module.nix;

      # Convenience overlay so callers can do `pkgs.homelab-mcp` after
      # adding `inputs.homelab-mcp.overlays.default` to their nixpkgs.
      overlays.default = final: _prev: {
        homelab-mcp = mkPackage final;
      };
    };
}
