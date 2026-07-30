# nix/sidecar.nix
#
# The Actual Budget sidecar: a small Node service that owns the
# `@actual-app/api` client so the Python finances_* tools can query the
# household budget over loopback. See ../sidecar/server.js for why it exists.
#
# VERSION PINNING IS LOAD-BEARING. `@actual-app/api` applies its bundled
# migrations to the budget file, so a client newer than the sync server
# migrates the file to a schema the server's own web UI can't read (this
# happened on 2026-07-30 via an unpinned `npx -y actual-budget-mcp`). The
# exact version lives in ../sidecar/package.json and is frozen by
# package-lock.json; buildNpmPackage refuses to resolve anything else.
# Before bumping it, confirm the sync server is already at that version.

{ lib
, buildNpmPackage
, nodejs
, python3
, pkg-config
, libsecret
, stdenv
, darwin
}:

buildNpmPackage {
  pname = "homelab-mcp-actual-sidecar";
  version = "0.1.0";

  src = ../sidecar;

  # Bump alongside package-lock.json: nix will print the expected value on
  # mismatch. `lib.fakeHash` regenerates it.
  npmDepsHash = "sha256-EvU/18E28smxSOjZXMryEPxye3ylzeYi09jH2zBFNl0=";

  inherit nodejs;

  # better-sqlite3 ships a native addon that is rebuilt from source here.
  nativeBuildInputs = [ python3 pkg-config ];
  buildInputs = [ libsecret ] ++ lib.optionals stdenv.isDarwin [
    darwin.apple_sdk.frameworks.Security
  ];

  # No build step: server.js is plain CommonJS.
  dontNpmBuild = true;

  installPhase = ''
    runHook preInstall

    mkdir -p $out/lib/sidecar $out/bin
    cp -r node_modules server.js package.json $out/lib/sidecar/

    makeWrapper ${lib.getExe nodejs} $out/bin/homelab-mcp-actual-sidecar \
      --add-flags "$out/lib/sidecar/server.js"

    runHook postInstall
  '';

  meta = with lib; {
    description = "Loopback HTTP bridge from homelab-mcp to Actual Budget";
    mainProgram = "homelab-mcp-actual-sidecar";
    license = licenses.mit;
    platforms = platforms.unix;
  };
}
