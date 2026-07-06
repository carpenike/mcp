"""Hatch build hook: fetch-at-build the pinned pocketid-mcp-as contract.

GitHub is the single source of truth. We do NOT commit the contract content
(contract.json / CONTRACT.md) into this source tree. Instead, at build time we
fetch them from the upstream repo at the ref pinned in ``contract/PINNED.json``,
stage them under ``contract/`` (gitignored), and force-include them into the
wheel as ``homelab_mcp/_vendored/`` so the installed package is self-contained
(the running server never depends on GitHub).

Offline-friendly + sandbox-friendly, but NOT blindly cache-trusting: staged
files are reused without touching the network ONLY when they match the pin.
Matching means their bytes hash to the per-file ``sha256`` digests recorded in
``PINNED.json`` (when present), or — if no digests are recorded — the resolved
ref stamped in ``contract/.ref`` equals the pinned ref. This closes a stale-
cache hole: a pin bump that wasn't followed by a refetch (so ``contract/``
still holds the OLD content) is now detected and refetched, instead of silently
embedding stale content into the wheel. Content supplied by the Nix build via
``fetchFromGitHub`` (the exact pinned tree) satisfies the hash check, so the
network-free Nix sandbox never tries to fetch.
"""

from __future__ import annotations

import hashlib
import json
import urllib.request
from pathlib import Path
from typing import Any

from hatchling.builders.hooks.plugin.interface import BuildHookInterface

_RAW = "https://raw.githubusercontent.com/carpenike/mcp-as-contract/{ref}/{name}"
_FILES = ("contract.json", "CONTRACT.md")


class ContractFetchHook(BuildHookInterface):
    """Stage + force-include the pinned contract files into the wheel."""

    PLUGIN_NAME = "custom"

    def initialize(self, version: str, build_data: dict[str, Any]) -> None:
        root = Path(self.root)
        contract_dir = root / "contract"
        pinned = json.loads((contract_dir / "PINNED.json").read_text(encoding="utf-8"))
        ref = pinned["ref"]
        hashes: dict[str, str] = pinned.get("sha256", {})
        stamp = contract_dir / ".ref"

        contract_dir.mkdir(parents=True, exist_ok=True)

        def _digest(path: Path) -> str:
            return hashlib.sha256(path.read_bytes()).hexdigest()

        def _staged_matches_pin() -> bool:
            for name in _FILES:
                dest = contract_dir / name
                if not dest.is_file():
                    return False
                want = hashes.get(name)
                if want is not None and _digest(dest) != want:
                    return False
            # Reaching here means every file exists and (if digests are
            # recorded) matched. With digests, that's sufficient. Without
            # them, fall back to the ref stamp so a pin bump without a
            # refetch is still caught.
            if hashes:
                return True
            return stamp.is_file() and stamp.read_text(encoding="utf-8").strip() == ref

        if not _staged_matches_pin():
            # Cache miss / stale / tampered: (re)fetch from the pin. Requires
            # network — in an offline sandbox with no valid cache this fails
            # loudly, which is the correct signal (don't ship stale content).
            for name in _FILES:
                url = _RAW.format(ref=ref, name=name)
                self.app.display_info(f"contract-fetch: pulling {url}")
                with urllib.request.urlopen(url, timeout=30) as resp:  # noqa: S310
                    data = resp.read()
                want = hashes.get(name)
                if want is not None:
                    got = hashlib.sha256(data).hexdigest()
                    if got != want:
                        raise RuntimeError(
                            f"contract-fetch: integrity check failed for {name} "
                            f"at ref {ref}: expected sha256 {want}, got {got}"
                        )
                (contract_dir / name).write_bytes(data)

        # Stamp the resolved ref so the next build can detect a pin bump even
        # when PINNED.json carries no content digests.
        stamp.write_text(ref + "\n", encoding="utf-8")

        for name in _FILES:
            dest = contract_dir / name
            # Bundle into the wheel as package data so the runtime hosting
            # routes can serve it without any GitHub round-trip.
            build_data["force_include"][str(dest)] = f"homelab_mcp/_vendored/{name}"
