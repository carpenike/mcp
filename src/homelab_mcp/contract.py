"""Public hosting of the pocketid-mcp-as contract (Part B).

mcp.holthome.net is the designated public home of the shared
`pocketid-mcp-as` contract. Other carpenike repos' build/CI harnesses
fetch the spec from here at runtime, so we expose two unauthenticated,
GET-only, CORS-open routes that live entirely OUTSIDE the OAuth/bearer
auth path (same posture as the `.well-known` OAuth discovery docs):

  GET /.well-known/mcp-as-contract.json  -> the machine-readable contract.json
  GET /contract                          -> the human-readable CONTRACT.md (raw)

The served bytes are a *fetch-at-build* copy of the upstream contract:
GitHub is the single source of truth, so the content is NOT committed to
this source tree. The wheel build (hatch_build.py) pulls contract.json +
CONTRACT.md from the ref pinned in `contract/PINNED.json` and bundles them
into the package, so the running server has no GitHub dependency. We do NOT
live-proxy GitHub. A CI check asserts the live-served contract.json
deep-equals upstream@pinned-ref.
"""

from __future__ import annotations

import json
import logging
from importlib.resources import files
from pathlib import Path
from typing import Any

from starlette.requests import Request
from starlette.responses import PlainTextResponse, Response
from starlette.routing import Route

log = logging.getLogger(__name__)

CONTRACT_JSON_PATH = "/.well-known/mcp-as-contract.json"
CONTRACT_MD_PATH = "/contract"

# Paths the JWT middleware must treat as public (no bearer required).
CONTRACT_PATHS: frozenset[str] = frozenset({CONTRACT_JSON_PATH, CONTRACT_MD_PATH})

_CACHE_CONTROL = "public, max-age=300"
_CORS_HEADER = "Access-Control-Allow-Origin"


def _find_contract_dir() -> Path:
    """Resolve the directory holding the contract.json + CONTRACT.md.

    The content is fetched-at-build (never committed). Two layouts are
    supported so the same code works in a production wheel and in an
    editable dev checkout:

      1. Production wheel: hatch_build.py force-includes the fetched files
         into the package as `homelab_mcp/_vendored/`.
      2. Dev checkout (editable install): the files are staged at the
         repo-root `contract/` directory (gitignored), fetched by the
         editable build hook or `make contract-pull`.
    """
    try:
        vendored = Path(str(files("homelab_mcp"))) / "_vendored"
        if (vendored / "contract.json").is_file():
            return vendored
    except (ModuleNotFoundError, FileNotFoundError):  # pragma: no cover - defensive
        pass

    repo_contract = Path(__file__).resolve().parent.parent.parent / "contract"
    if (repo_contract / "contract.json").is_file():
        return repo_contract

    raise FileNotFoundError(
        "vendored contract files not found; expected homelab_mcp/_vendored/ "
        "(wheel) or <repo>/contract/ (dev checkout). Run scripts/pull-contract.sh."
    )


class _LoadedContract:
    """The vendored contract bytes + parsed version, loaded once at startup."""

    def __init__(self) -> None:
        contract_dir = _find_contract_dir()
        self.json_bytes = (contract_dir / "contract.json").read_bytes()
        self.markdown_text = (contract_dir / "CONTRACT.md").read_text(encoding="utf-8")
        parsed: dict[str, Any] = json.loads(self.json_bytes)
        self.version = str(parsed.get("version", "unknown"))


def build_routes() -> list[Route]:
    """Return the unauthenticated, GET-only, CORS-open contract-hosting routes."""
    loaded = _LoadedContract()
    log.info(
        "Hosting pocketid-mcp-as contract v%s at %s and %s",
        loaded.version,
        CONTRACT_JSON_PATH,
        CONTRACT_MD_PATH,
    )

    common_headers = {
        _CORS_HEADER: "*",
        "Cache-Control": _CACHE_CONTROL,
        # Surfaced so a fetcher can sanity-check the version without
        # parsing the body.
        "X-Contract-Version": loaded.version,
    }

    def _preflight() -> Response:
        return Response(
            status_code=204,
            headers={
                _CORS_HEADER: "*",
                "Access-Control-Allow-Methods": "GET, OPTIONS",
                "Access-Control-Allow-Headers": "*",
                "Cache-Control": _CACHE_CONTROL,
            },
        )

    async def contract_json(request: Request) -> Response:
        if request.method == "OPTIONS":
            return _preflight()
        # Serve the exact fetched bytes so the CI drift guard (served ==
        # upstream@pinned-ref) is a byte-for-byte comparison.
        return Response(
            content=loaded.json_bytes,
            media_type="application/json",
            headers=common_headers,
        )

    async def contract_markdown(request: Request) -> Response:
        if request.method == "OPTIONS":
            return _preflight()
        return PlainTextResponse(
            loaded.markdown_text,
            media_type="text/markdown; charset=utf-8",
            headers=common_headers,
        )

    return [
        Route(CONTRACT_JSON_PATH, contract_json, methods=["GET", "OPTIONS"]),
        Route(CONTRACT_MD_PATH, contract_markdown, methods=["GET", "OPTIONS"]),
    ]
