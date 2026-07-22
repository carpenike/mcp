"""Tool module auto-discovery.

Convention: every file in this package (other than `_registry` and any
file starting with `_`) that exports a top-level `register()` function
will have it called at app startup. This is what makes "one server,
many namespaced tool categories" cheap to extend — new categories don't
need to touch the central app.

The `register()` callable may take either two or three positional
arguments:

    def register(mcp, settings) -> None: ...                 # most tools
    def register(mcp, settings, mint_token) -> None: ...     # tools that
                                                             # need to make
                                                             # outbound calls
                                                             # to OTHER MCP-
                                                             # protected
                                                             # resources

The optional third arg (`mint_token`) is a callable that re-mints a
short-TTL RS256 JWT addressed to a downstream resource (see HOF-004 /
`oauth_provider.mint_tool_hop_token`). Tools that don't need it (cooklang,
gatus, etc.) keep the two-arg signature and ignore the rest.

See `AGENTS.md` for the naming conventions and security non-negotiables.
"""

from __future__ import annotations

import inspect
import logging
from collections.abc import Callable
from importlib import import_module
from pkgutil import iter_modules
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from mcp.server.fastmcp import FastMCP

    from homelab_mcp.config import Settings

log = logging.getLogger(__name__)

# A `mint_token` callable mirrors `oauth_provider.mint_tool_hop_token`'s
# kwargs signature — kept as Any here to avoid pulling oauth_provider
# into the registry (and to let tests substitute fakes).
MintTokenFn = Callable[..., str]


def collect_instructions() -> str | None:
    """Concatenate the optional module-level `INSTRUCTIONS` strings.

    The result is passed to FastMCP's `instructions` parameter, which MCP
    clients receive at connection time — server-level usage guidance that
    spans tools (workflows, freshness caveats), where per-tool
    descriptions can't reach. Same isolation semantics as register_all:
    a module that fails to import contributes nothing rather than
    aborting the rest. Must run BEFORE FastMCP construction, so it walks
    the package independently.
    """
    import homelab_mcp.tools as tools_pkg

    sections: list[str] = []
    for _finder, modname, _ispkg in iter_modules(tools_pkg.__path__):
        if modname.startswith("_"):
            continue
        try:
            mod = import_module(f"{tools_pkg.__name__}.{modname}")
        except Exception:
            # register_all logs the same failure loudly; debug here avoids
            # double-reporting while keeping a trace for this code path.
            log.debug("collect_instructions: %s failed to import — skipping", modname)
            continue
        text = getattr(mod, "INSTRUCTIONS", None)
        if isinstance(text, str) and text.strip():
            sections.append(text.strip())
    return "\n\n".join(sections) or None


def register_all(
    mcp: FastMCP,
    settings: Settings,
    mint_token: MintTokenFn | None = None,
) -> None:
    """Walk this package, importing each non-underscore module and calling its register().

    If `mint_token` is provided and a module's `register()` accepts a
    third positional argument, the callable is passed through so the
    module can mint per-call JWTs for outbound resource calls. Modules
    with the legacy two-arg signature receive only `(mcp, settings)`.
    """
    import homelab_mcp.tools as tools_pkg

    registered: list[str] = []
    skipped: list[str] = []

    for _finder, modname, _ispkg in iter_modules(tools_pkg.__path__):
        if modname.startswith("_"):
            continue
        full = f"{tools_pkg.__name__}.{modname}"
        # Isolate each category: an import error or a raising register() in one
        # module must not abort discovery of the others. A single broken
        # category degrades to "that category is unavailable", not "the server
        # has no tools".
        try:
            mod = import_module(full)
        except Exception:
            log.exception("tool module %s failed to import — skipping", full)
            skipped.append(modname)
            continue
        register_fn = getattr(mod, "register", None)
        if not callable(register_fn):
            log.warning("tool module %s has no register() function — skipping", full)
            skipped.append(modname)
            continue

        # Dispatch by arity. inspect lets us support both signatures
        # without making every tool take a parameter it doesn't use.
        params = inspect.signature(register_fn).parameters
        positional = [
            p
            for p in params.values()
            if p.kind
            in (inspect.Parameter.POSITIONAL_ONLY, inspect.Parameter.POSITIONAL_OR_KEYWORD)
        ]
        args: list[Any] = [mcp, settings]
        if len(positional) >= 3:
            if mint_token is None:
                log.warning(
                    "tool module %s declares a 3-arg register() but no mint_token was "
                    "provided — skipping (likely a misconfiguration in app.py)",
                    full,
                )
                skipped.append(modname)
                continue
            args.append(mint_token)
        try:
            register_fn(*args)
        except Exception:
            log.exception("tool module %s register() raised — skipping", full)
            skipped.append(modname)
            continue
        registered.append(modname)

    if not registered:
        log.error("no tool modules registered — homelab-mcp will be useless")
    else:
        log.info("registered tool modules: %s", ", ".join(registered))
    if skipped:
        log.info("skipped (no register() defined): %s", ", ".join(skipped))
