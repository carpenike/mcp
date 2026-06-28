"""Hatch build hook: fetch-at-build the pinned pocketid-mcp-as contract.

GitHub is the single source of truth. We do NOT commit the contract content
(contract.json / CONTRACT.md) into this source tree. Instead, at build time we
fetch them from the upstream repo at the ref pinned in ``contract/PINNED.json``,
stage them under ``contract/`` (gitignored), and force-include them into the
wheel as ``homelab_mcp/_vendored/`` so the installed package is self-contained
(the running server never depends on GitHub).

Offline-friendly + sandbox-friendly: if the staged files already exist (fetched
by a prior build, by ``make contract-pull``, or supplied by the Nix build via
``fetchFromGitHub``) the hook uses them as-is and never touches the network.
"""

from __future__ import annotations

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

        contract_dir.mkdir(parents=True, exist_ok=True)
        for name in _FILES:
            dest = contract_dir / name
            if not dest.is_file():
                url = _RAW.format(ref=ref, name=name)
                self.app.display_info(f"contract-fetch: pulling {url}")
                with urllib.request.urlopen(url, timeout=30) as resp:  # noqa: S310
                    dest.write_bytes(resp.read())
            # Bundle into the wheel as package data so the runtime hosting
            # routes can serve it without any GitHub round-trip.
            build_data["force_include"][str(dest)] = f"homelab_mcp/_vendored/{name}"
