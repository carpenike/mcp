"""Runtime access to the build revision (git SHA), if any was baked in.

The Nix build stages ``HOMELAB_MCP_GIT_REV`` at build time; a runtime read
of a build-time env var is empty, so the packaging step writes it into a
``homelab_mcp/_vendored/buildinfo.txt`` data file instead. This module reads
whichever source is available and degrades to ``"unknown"`` — it must never
raise, since it's on the /healthz path.
"""

from __future__ import annotations

import os
from importlib.resources import files


def build_revision() -> str:
    """Return the git revision the running package was built from, or 'unknown'."""
    env = os.environ.get("HOMELAB_MCP_GIT_REV")
    if env:
        return env
    try:
        vendored = files("homelab_mcp") / "_vendored" / "buildinfo.txt"
        text = vendored.read_text(encoding="utf-8").strip()
        if text:
            return text
    except (FileNotFoundError, ModuleNotFoundError, OSError):
        pass
    return "unknown"
