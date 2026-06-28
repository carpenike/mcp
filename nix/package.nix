# nix/package.nix
#
# Builds the homelab-mcp Python application. Called from flake.nix as
#   pkgs.python313Packages.callPackage ./nix/package.nix { ... }
{ lib
, buildPythonApplication
, fetchFromGitHub
, hatchling
, mcp
, starlette
, uvicorn
, httpx
, authlib
, itsdangerous
, pyjwt
, cryptography
, pydantic
, pydantic-settings
, gitRev ? "unknown"
, buildTime ? "unknown"
}:

let
  # Fetch-at-build supplies the pocketid-mcp-as contract content from GitHub
  # (single source of truth). The Nix sandbox has no network, so we pin the
  # contract repo here with a content hash and stage the files where
  # hatch_build.py expects them; the hook then sees them present and skips its
  # own network fetch. Keep `rev` in sync with contract/PINNED.json (.ref).
  contractSrc = fetchFromGitHub {
    owner = "carpenike";
    repo = "mcp-as-contract";
    rev = "v1.1.0";
    hash = "sha256-TCeq3AaIF6j4+x+GYgUOBDvQsMIVTlG+IbEKLG0m18M=";
  };
in
buildPythonApplication {
  pname = "homelab-mcp";
  version = "0.1.0";

  pyproject = true;

  src = lib.cleanSource ../.;

  build-system = [ hatchling ];

  # Stage the pinned contract content before the hatch build runs, so the
  # fetch-at-build hook is a no-op inside the network-free Nix sandbox.
  postPatch = ''
    mkdir -p contract
    cp ${contractSrc}/contract.json contract/contract.json
    cp ${contractSrc}/CONTRACT.md  contract/CONTRACT.md
  '';

  dependencies = [
    mcp
    starlette
    uvicorn
    httpx
    authlib
    itsdangerous
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
