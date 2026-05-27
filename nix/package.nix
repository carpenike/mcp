# nix/package.nix
#
# Builds the homelab-mcp Python application. Called from flake.nix as
#   pkgs.python313Packages.callPackage ./nix/package.nix { ... }
{ lib
, buildPythonApplication
, hatchling
, mcp
, starlette
, uvicorn
, httpx
, pyjwt
, cryptography
, pydantic
, pydantic-settings
, gitRev ? "unknown"
, buildTime ? "unknown"
}:

buildPythonApplication {
  pname = "homelab-mcp";
  version = "0.1.0";

  pyproject = true;

  src = lib.cleanSource ../.;

  build-system = [ hatchling ];

  dependencies = [
    mcp
    starlette
    uvicorn
    httpx
    pyjwt
    cryptography
    pydantic
    pydantic-settings
  ];

  # Build-time identity baked into env vars. We don't have a runtime
  # `--version` flag yet but the data is staged so a future status
  # endpoint can surface it.
  HOMELAB_MCP_GIT_REV = gitRev;
  HOMELAB_MCP_BUILD_TIME = buildTime;

  # Skip Nix-sandbox checks; tests run in CI against the source tree
  # (most need network mocking which is fiddly inside Nix's sandbox).
  doCheck = false;

  meta = with lib; {
    description = "Homelab MCP server exposing cooklang + gatus as Claude tools";
    homepage = "https://github.com/carpenike/mcp";
    license = licenses.mit;
    mainProgram = "homelab-mcp";
    platforms = [ "x86_64-linux" "aarch64-linux" "aarch64-darwin" ];
  };
}
