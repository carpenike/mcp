"""Tool module auto-discovery.

Convention: every file in this package (other than `_registry` and any
file starting with `_`) that exports a top-level `register(mcp, settings)`
function will have it called at app startup. This is what makes "one
server, many namespaced tool categories" cheap to extend — new categories
don't need to touch the central app.

See `AGENTS.md` for the naming conventions and security non-negotiables.
"""

from __future__ import annotations

import logging
from importlib import import_module
from pkgutil import iter_modules
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from mcp.server.fastmcp import FastMCP

    from homelab_mcp.config import Settings

log = logging.getLogger(__name__)


def register_all(mcp: FastMCP, settings: Settings) -> None:
    """Walk this package, importing each non-underscore module and calling its register()."""
    import homelab_mcp.tools as tools_pkg

    registered: list[str] = []
    skipped: list[str] = []

    for _finder, modname, _ispkg in iter_modules(tools_pkg.__path__):
        if modname.startswith("_"):
            continue
        full = f"{tools_pkg.__name__}.{modname}"
        mod = import_module(full)
        register_fn = getattr(mod, "register", None)
        if not callable(register_fn):
            log.warning("tool module %s has no register() function — skipping", full)
            skipped.append(modname)
            continue
        register_fn(mcp, settings)
        registered.append(modname)

    if not registered:
        log.error("no tool modules registered — homelab-mcp will be useless")
    else:
        log.info("registered tool modules: %s", ", ".join(registered))
    if skipped:
        log.info("skipped (no register() defined): %s", ", ".join(skipped))
