"""Tool category package.

Each module here that exports a top-level `register(mcp, settings)`
function will be auto-discovered at startup. See `_registry.py` for the
discovery mechanism and `AGENTS.md` for the conventions.
"""

from homelab_mcp.tools._registry import collect_instructions, register_all

__all__ = ["collect_instructions", "register_all"]
