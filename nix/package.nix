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
  # hatch_build.py expects them; the hook then verifies their sha256 against
  # contract/PINNED.json and, seeing a match, skips its own network fetch.
  # `rev` MUST equal contract/PINNED.json `.ref` (the immutable commit SHA) —
  # CI asserts this. This is v1.2.0 content pinned to the main commit it landed
  # on (v1.2.0 was untagged at pin time; see PINNED.json `.tag`).
  contractSrc = fetchFromGitHub {
    owner = "carpenike";
    repo = "mcp-as-contract";
    rev = "7d9a8b40093cd169cf5d0e9ecc82be6cb64ece83";
    hash = "sha256-HJRNBMPTw6z3ezt8aTvZAGU1PKycNaFoA1ctP8LKHTo=";
  };
in
buildPythonApplication {
  pname = "homelab-mcp";
  # Keep in sync with pyproject.toml [project].version and
  # src/homelab_mcp/__init__.py __version__.
  version = "0.4.0";

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
    pyjwt
    cryptography
    pydantic
    pydantic-settings
  ];

  # Build-time identity, passed as *derivation* environment variables. NOTE:
  # these exist only for the build process — they are NOT baked into the
  # installed package and are invisible to the running server (systemd starts
  # it with a clean environment). Surfacing them at runtime needs an app-side
  # read (e.g. a /healthz or --version handler in src/ that reads a bundled
  # data file or a compile-time constant); until that lands these are
  # effectively inert. Left in place so that app-side work has the values
  # available to wire through.
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
