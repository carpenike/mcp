"""Grocy stock & inventory tools — a conversation-first surface.

Grocy (grocy.holthome.net) is the system of record for household food
inventory. These tools are how the assistant reads and writes that record
in natural dialogue — the defining use case is walking the freezer/fridge/
pantry and narrating what's there ("two ribeyes in the chest freezer"),
plus planning reads ("what's expiring this week?").

Deliberately NARROW. We expose only product master data, stock actions,
and the minimal location + quantity-unit setup needed to support them on a
fresh instance. Grocy's recipes, meal planning, shopping list, chores,
tasks, batteries and equipment are intentionally NOT wrapped — they are
either owned by other systems or out of scope (see the build handoff).

Grocy splits **master data** (`/objects/*` CRUD) from **stock actions**
(`/stock/*`). A product must exist (with a location + quantity unit) before
it is stockable, so bootstrap order is: units + locations → products →
stock. `grocy_stock_item` composes find-or-create + a stock action so a
walkthrough is a single call per item.

Authentication: every request carries the `GROCY-API-KEY` header, loaded
from `Settings.grocy_api_key` (sops-managed env var). It NEVER comes from
user input and is NEVER logged.

NOTE on product-create fields: built from grocy/grocy's canonical OpenAPI
(master). Quantity-unit handling shifted across Grocy 3.x→4.x; if a create
fails with a column/FK error on the deployed instance, reconcile the body
in `_create_product` against that instance's live OpenAPI (/api).

Tool name convention: `grocy_<verb>_<object>`. See AGENTS.md.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import UTC, date, datetime, timedelta
from typing import TYPE_CHECKING, Annotated, Any

import httpx
from pydantic import Field

from homelab_mcp.tools._http import ToolError, enc, make_client, request_json

if TYPE_CHECKING:
    from mcp.server.fastmcp import FastMCP

    from homelab_mcp.config import Settings

log = logging.getLogger(__name__)

# Bound on how long we wait for any single Grocy call.
_TIMEOUT = 15

# Cap on concurrent per-item enrichment fan-outs, so a wide read (many
# products / many below-minimum rows) pools its follow-up calls instead of
# issuing them all at once. Mirrors cooklang's `_scan_ingredients`.
_ENRICH_CONCURRENCY = 8

# Idempotent seed for a blank instance (see handoff §7.2). Plurals default
# to the singular — Grocy requires name_plural but our units read naturally
# either way ("2 lb", "2 count").
_SEED_LOCATIONS = ["Chest Freezer", "Kitchen Fridge", "Garage Fridge", "Pantry"]
_SEED_UNITS = ["count", "lb", "oz", "pack"]

# Upper bound on stock-log rows pulled for a consumption-history window, so a
# long-lived product can't fan a single read into an unbounded response.
_MAX_LOG_ROWS = 2000

# Single source of truth for the quantity-unit-conversion row shape, shared by
# the reader (`_find_conversion`) and the writer (`grocy_set_unit_conversion`)
# so the two can never drift. Verified against Grocy 4.6.0. If a future version
# renames these columns, change them HERE and both sides follow.
_CONV_ENTITY = "quantity_unit_conversions"
_CONV_FROM = "from_qu_id"
_CONV_TO = "to_qu_id"
_CONV_FACTOR = "factor"
_CONV_PRODUCT = "product_id"

# A store's address is kept in a DEDICATED Grocy userfield rather than
# overloading the free-text `description` column — so `description` stays
# available for other notes. The userfield definition is auto-created on first
# use (a one-time schema addition on the shopping_locations entity).
_SHOPPING_LOCATIONS = "shopping_locations"
_STORE_ADDRESS_FIELD = "address"


# Grocy's structured error is the shared `ToolError` — same
# ``{"error": {code, message, hint}}`` payload shape. Kept under the local
# name `_GrocyError` so the module's many `except _GrocyError` boundaries and
# `raise _GrocyError(code, message, hint)` sites read unchanged.
_GrocyError = ToolError


class _Disambiguation(Exception):  # noqa: N818 - a control-flow signal, not an error
    """A name matched products but not exactly one — surfaced for the assistant.

    Carries the candidate list so the conversation can resolve it and re-call
    with a concrete id. Mirrors the find-or-create contract of the write tools
    so the read tools never guess which product was meant.
    """

    def __init__(self, name: str, candidates: list[dict[str, Any]]) -> None:
        super().__init__(f"'{name}' is ambiguous")
        self.name = name
        self.candidates = candidates

    def payload(self) -> dict[str, Any]:
        return {
            "error": {
                "code": "needs_disambiguation",
                "message": f"'{self.name}' matches multiple products; not guessing.",
                "hint": "Re-call with product set to the intended product's id.",
            },
            "candidates": self.candidates,
        }


# The shared path-segment encoder, re-exported under the module's historical
# name so `_enc(...)` call sites (and the helper's unit tests) keep working.
_enc = enc


def _compact(body: dict[str, Any]) -> dict[str, Any]:
    """Drop keys whose value is None so we only send fields the caller set."""
    return {k: v for k, v in body.items() if v is not None}


def _norm(value: Any) -> str:
    """Trim + lowercase for case-insensitive name comparison."""
    return str(value or "").strip().lower()


def _like_escape(value: str) -> str:
    """Escape SQL-LIKE metacharacters for a Grocy ``name~`` query filter.

    Grocy translates ``name~value`` into ``name LIKE %value%`` (SQL LIKE
    semantics), so a raw ``%`` or ``_`` in the value acts as a wildcard and
    a backslash can break the pattern. We backslash-escape those so a spoken
    name containing them is searched as a literal substring rather than
    over-matching (or 400-ing). Exact-match layering still guards correctness;
    this keeps the raw substring search robust.
    """
    return value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


async def _map_bounded(
    fn: Any, items: list[dict[str, Any]], concurrency: int = _ENRICH_CONCURRENCY
) -> list[dict[str, Any]]:
    """Apply an async `fn` over `items` concurrently (bounded), preserving order.

    `asyncio.gather` keeps result order aligned to the input, so a fan-out of
    per-item enrichment stays ordered while pooling its calls behind a
    semaphore. A `_GrocyError` from any task propagates (caught at the tool
    boundary) exactly as the old sequential loop would have raised.
    """
    sem = asyncio.Semaphore(concurrency)

    async def _run(item: dict[str, Any]) -> dict[str, Any]:
        async with sem:
            return await fn(item)  # type: ignore[no-any-return]

    return list(await asyncio.gather(*(_run(i) for i in items)))


def _extract_grocy_version(info: Any) -> str | None:
    """Pull a version string out of a /system/info payload across known shapes.

    Grocy has shipped the version as ``grocy_version.Version`` (capital),
    ``grocy_version.version``, a bare ``grocy_version`` string, or a top-level
    ``version`` / ``release_version``. Returns None if none are present (or the
    payload isn't a dict).
    """
    if not isinstance(info, dict):
        return None
    gv = info.get("grocy_version")
    if isinstance(gv, dict):
        v = gv.get("Version") or gv.get("version")
        if v:
            return str(v)
    elif isinstance(gv, str) and gv:
        return gv
    top = info.get("version") or info.get("release_version")
    return str(top) if top else None


def register(mcp: FastMCP, settings: Settings) -> None:
    """Register grocy_* stock/inventory tools on the given MCP server."""
    api_key = settings.grocy_api_key

    # The Grocy REST API lives under `/api`, while tool paths are written as
    # `/objects/...`, `/stock/...`, `/system/info`. Compute the API base ONCE
    # so it works whether or not the configured base already ends in `/api`
    # (config default is `https://grocy.holthome.net`, no `/api`).
    _raw_base = settings.grocy_base_url.rstrip("/")
    api_base = _raw_base if _raw_base.endswith("/api") else _raw_base + "/api"

    # ONE pooled client, reused across every call so TLS handshakes and
    # connections are pooled instead of rebuilt per request. Constructing it
    # outside the event loop is fine — httpx binds to the loop on first use.
    client = make_client(headers={"GROCY-API-KEY": api_key}, timeout=_TIMEOUT)

    # ── core HTTP ───────────────────────────────────────────────────
    async def _call(
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        json: dict[str, Any] | None = None,
    ) -> Any:
        """One authenticated Grocy call. Returns parsed JSON or None (204).

        Raises `_GrocyError` (never to the transport) on any failure.
        """
        if not api_key:
            raise _GrocyError(
                "grocy_unreachable",
                "Grocy API key is not configured.",
                "Set HOMELAB_MCP_GROCY_API_KEY (Grocy → Settings → Manage API keys).",
            )
        return await request_json(
            client,
            method,
            f"{api_base}{path}",
            service="grocy",
            params=params,
            json=json,
            unreachable_hint="Check HOMELAB_MCP_GROCY_BASE_URL and that the instance is up.",
        )

    async def _probe(path: str) -> dict[str, Any]:
        """Raw GET for diagnostics — returns status/headers/body WITHOUT raising
        or nulling an empty body (unlike `_call`). Used by `grocy_health` so a
        misbehaving endpoint reports its actual HTTP status + body, not just a
        swallowed None.
        """
        if not api_key:
            return {"connect_error": "no_api_key"}
        try:
            resp = await client.get(f"{api_base}{path}")
        except httpx.HTTPError as exc:
            return {"connect_error": exc.__class__.__name__}
        body = resp.text or ""
        out: dict[str, Any] = {
            "status": resp.status_code,
            "content_type": resp.headers.get("content-type"),
            "body_excerpt": body[:500],
        }
        try:
            out["json"] = resp.json() if body else None
        except ValueError:
            out["json"] = None
        return out

    # ── master-data helpers ─────────────────────────────────────────
    async def _search(entity: str, name: str) -> list[dict[str, Any]]:
        """LIKE-search an /objects/{entity} collection by name (substring).

        The name is LIKE-escaped so `%`/`_`/`\\` in a spoken name search as
        literals instead of wildcards (Grocy uses SQL LIKE for `~`).
        """
        data = await _call(
            "GET", f"/objects/{entity}", params={"query[]": f"name~{_like_escape(name)}"}
        )
        return data if isinstance(data, list) else []

    async def _list_all(entity: str) -> list[dict[str, Any]]:
        """Fetch a whole /objects/{entity} collection (no server-side filter)."""
        data = await _call("GET", f"/objects/{entity}")
        return data if isinstance(data, list) else []

    async def _resolve_id(entity: str, value: str | int, not_found_code: str) -> int:
        """Resolve a name-or-id to an existing id; raise if a name has no exact match.

        Exact resolution does NOT use the server-side `~` LIKE filter: we fetch
        the (small) master-data collection unfiltered and match client-side.
        Relying on `~` here was fragile — it could return a set that omitted the
        exact row for some inputs, raising a spurious `*_not_found` for an object
        that demonstrably exists. Master-data collections (locations, units,
        stores) are tiny, so the unfiltered read is cheap and deterministic.
        """
        if isinstance(value, int):
            return value
        s = str(value).strip()
        if s.isdigit():
            return int(s)
        rows = await _list_all(entity)
        exact = [i for i in rows if _norm(i.get("name")) == _norm(s)]
        singular = entity.rstrip("s").replace("_", " ")
        if len(exact) == 1:
            return int(exact[0]["id"])
        if len(exact) > 1:
            raise _GrocyError(
                not_found_code, f"Multiple {entity} named '{s}'.", "Pass the numeric id instead."
            )
        log.warning(
            "grocy: no %s exactly named %r among %d rows: %s",
            entity,
            s,
            len(rows),
            [r.get("name") for r in rows][:20],
        )
        raise _GrocyError(
            not_found_code,
            f"No {singular} named '{s}'.",
            "Create it first with the matching ensure_* tool.",
        )

    async def _ensure(entity: str, name: str, create_body: dict[str, Any]) -> dict[str, Any]:
        """Idempotent lookup-or-create on an /objects/{entity} collection.

        Uses an unfiltered exact-match lookup (see `_resolve_id`) so a `~`-filter
        miss can't cause a duplicate row to be created for an existing object.
        """
        exact = [i for i in await _list_all(entity) if _norm(i.get("name")) == _norm(name)]
        if exact:
            return {"id": int(exact[0]["id"]), "name": exact[0].get("name"), "created": False}
        res = await _call("POST", f"/objects/{entity}", json=create_body)
        new_id = res.get("created_object_id") if isinstance(res, dict) else None
        return {"id": new_id, "name": name, "created": True}

    # ── userfields (Grocy's custom-field mechanism) ─────────────────
    async def _userfield_values(entity: str, object_id: int) -> dict[str, Any]:
        """Current userfield values for one object, or {} (tolerant of none)."""
        try:
            data = await _call("GET", f"/userfields/{entity}/{object_id}")
        except _GrocyError:
            return {}
        return data if isinstance(data, dict) else {}

    async def _ensure_userfield(
        entity: str, name: str, caption: str, ftype: str = "text-single-line"
    ) -> None:
        """Idempotently define a userfield on an entity (one-time schema addition)."""
        defs = await _list_all("userfields")
        if any(d.get("entity") == entity and _norm(d.get("name")) == _norm(name) for d in defs):
            return
        await _call(
            "POST",
            "/objects/userfields",
            json={"entity": entity, "name": name, "caption": caption, "type": ftype},
        )

    async def _set_userfield(entity: str, object_id: int, name: str, value: str) -> None:
        await _call("PUT", f"/userfields/{entity}/{object_id}", json={name: value})

    async def _stock_detail(product_id: int) -> dict[str, Any] | None:
        try:
            d = await _call("GET", f"/stock/products/{product_id}")
        except _GrocyError:
            return None
        return d if isinstance(d, dict) else None

    async def _amount_on_hand(product_id: int) -> float | None:
        d = await _stock_detail(product_id)
        if d is None:
            return None
        amt = d.get("stock_amount")
        return float(amt) if amt is not None else None

    async def _enrich(product: dict[str, Any]) -> dict[str, Any]:
        """Fold current stock detail onto a master product for read-back."""
        pid = int(product["id"])
        out: dict[str, Any] = {"id": pid, "name": product.get("name")}
        detail = await _stock_detail(pid)
        if detail is not None:
            qu = detail.get("quantity_unit_stock") or {}
            loc = detail.get("location") or detail.get("default_location") or {}
            out["amount_on_hand"] = detail.get("stock_amount")
            out["stock_unit"] = qu.get("name")
            out["next_due_date"] = detail.get("next_due_date")
            out["default_location"] = loc.get("name")
        return out

    async def _create_product(name: str, location_id: int, unit_id: int) -> dict[str, Any]:
        """Create a master product with the SAME unit for stock/purchase/consume/price.

        Grocy 4.x products carry four quantity-unit references. A priced `add`
        resolves the product's PRICE (and consume) quantity unit, so a product
        created with only `qu_id_stock`/`qu_id_purchase` set has a null
        `qu_id_price` and a priced add fails on the missing relation — while a
        non-priced inventory (`set`) succeeds. Setting all four to the same unit
        (factor 1) is the standard "simple product" shape the Grocy UI creates
        and keeps priced intake working. See the module-level NOTE if a deployed
        instance rejects any of these fields.
        """
        body = {
            "name": name.strip(),
            "location_id": location_id,
            "qu_id_stock": unit_id,
            "qu_id_purchase": unit_id,
            "qu_id_consume": unit_id,
            "qu_id_price": unit_id,
            "min_stock_amount": 0,
        }
        res = await _call("POST", "/objects/products", json=body)
        new_id = res.get("created_object_id") if isinstance(res, dict) else None
        if new_id is None:
            raise _GrocyError("missing_fk", "Grocy did not return a new product id.", "")
        return {"id": int(new_id), "name": name.strip(), "created": True}

    # ── read-enrichment helpers (resolution reuse; never create) ────
    async def _resolve_product(value: str | int) -> dict[str, Any]:
        """Resolve a product by id or name for READ tools (never creates).

        Raises `_GrocyError("product_not_found")` or `_Disambiguation` so the
        caller can surface the typed error / candidate list.
        """
        if isinstance(value, int) or str(value).strip().isdigit():
            pid = int(value)
            try:
                found = await _call("GET", f"/objects/products/{pid}")
            except _GrocyError as exc:
                raise _GrocyError(
                    "product_not_found", f"No product with id {pid}.", "Check the id."
                ) from exc
            if not isinstance(found, dict) or not found.get("id"):
                raise _GrocyError(
                    "product_not_found", f"No product with id {pid}.", "Check the id."
                )
            return {"id": int(found["id"]), "name": found.get("name")}
        name = str(value).strip()
        if not name:
            raise _GrocyError("product_not_found", "No product given.", "Pass a name or id.")
        candidates = await _search("products", name)
        exact = [c for c in candidates if _norm(c.get("name")) == _norm(name)]
        if len(exact) == 1:
            return {"id": int(exact[0]["id"]), "name": exact[0].get("name")}
        if len(exact) > 1 or candidates:
            raise _Disambiguation(name, [await _enrich(c) for c in candidates[:10]])
        raise _GrocyError(
            "product_not_found",
            f"No product named '{name}'.",
            "Check the name or use grocy_find_products.",
        )

    async def _resolve_unit(value: str | int) -> tuple[int, str | None]:
        """Resolve a quantity unit to (id, name); raise unit_not_found on a bad name."""
        if isinstance(value, int) or str(value).strip().isdigit():
            uid = int(value)
            try:
                rec = await _call("GET", f"/objects/quantity_units/{uid}")
            except _GrocyError as exc:
                raise _GrocyError(
                    "unit_not_found", f"No quantity unit with id {uid}.", "Check the id."
                ) from exc
            return uid, (rec.get("name") if isinstance(rec, dict) else None)
        s = str(value).strip()
        exact = [i for i in await _search("quantity_units", s) if _norm(i.get("name")) == _norm(s)]
        if len(exact) == 1:
            return int(exact[0]["id"]), exact[0].get("name")
        if len(exact) > 1:
            raise _GrocyError("unit_not_found", f"Multiple units named '{s}'.", "Pass the id.")
        raise _GrocyError(
            "unit_not_found",
            f"No quantity unit named '{s}'.",
            "Create it with grocy_ensure (kind='unit').",
        )

    async def _query_conversions(from_id: int, to_id: int) -> list[dict[str, Any]]:
        """All conversion rows for a (from_unit, to_unit) pair — global + product."""
        rows = await _call(
            "GET",
            f"/objects/{_CONV_ENTITY}",
            params={"query[]": [f"{_CONV_FROM}={from_id}", f"{_CONV_TO}={to_id}"]},
        )
        return rows if isinstance(rows, list) else []

    async def _find_conversion(
        product_id: int, from_id: int, to_id: int
    ) -> tuple[float, str] | None:
        """Resolve a QU conversion factor: product-specific → global → reverse.

        Returns (factor, source) where amount_out = amount_in * factor, or None
        when no direct conversion exists (we do NOT chain or assume 1:1). Reads
        the shared `_CONV_*` row shape that `grocy_set_unit_conversion` writes.
        """

        def _pick(rows: list[dict[str, Any]]) -> tuple[float, str] | None:
            specific = [r for r in rows if str(r.get(_CONV_PRODUCT) or "") == str(product_id)]
            if specific:
                return float(specific[0][_CONV_FACTOR]), "product_specific"
            glob = [r for r in rows if not r.get(_CONV_PRODUCT)]
            if glob:
                return float(glob[0][_CONV_FACTOR]), "global"
            return None

        hit = _pick(await _query_conversions(from_id, to_id))
        if hit:
            return hit
        rhit = _pick(await _query_conversions(to_id, from_id))
        if rhit and rhit[0]:
            return 1.0 / rhit[0], rhit[1]
        return None

    async def _conversion_rows(pid: int | None) -> list[dict[str, Any]]:
        """Projected conversion rows for inspection: global + (optionally) one
        product's specific rows, unit/product ids resolved to names.
        """
        rows = await _call("GET", f"/objects/{_CONV_ENTITY}")
        unit_names = await _name_map("quantity_units")
        prod_names = await _name_map("products")
        out: list[dict[str, Any]] = []
        for r in rows if isinstance(rows, list) else []:
            raw_pid = r.get(_CONV_PRODUCT)
            rpid = int(raw_pid) if raw_pid not in (None, "") else None
            # Keep global rows (rpid is None) always; keep product-specific rows
            # only for the requested product when one is given.
            if pid is not None and rpid is not None and rpid != pid:
                continue
            fid = r.get(_CONV_FROM)
            tid = r.get(_CONV_TO)
            out.append(
                {
                    "conversion_id": r.get("id"),
                    "product": {"id": rpid, "name": prod_names.get(rpid)} if rpid else None,
                    "from_unit": unit_names.get(int(fid)) if fid is not None else None,
                    "to_unit": unit_names.get(int(tid)) if tid is not None else None,
                    "factor": r.get(_CONV_FACTOR),
                }
            )
        return out

    async def _name_map(entity: str) -> dict[int, str | None]:
        """id → name map for a whole /objects/{entity} collection."""
        items = await _call("GET", f"/objects/{entity}")
        rows = items if isinstance(items, list) else []
        return {int(i["id"]): i.get("name") for i in rows if i.get("id") is not None}

    async def _currency() -> str | None:
        """Grocy's configured currency code, or None if unavailable."""
        try:
            cfg = await _call("GET", "/system/config")
        except _GrocyError:
            return None
        if isinstance(cfg, dict):
            return cfg.get("CURRENCY") or cfg.get("currency")
        return None

    # ─────────────────────────────────────────────────────────────────
    # Diagnostics / bootstrap
    # ─────────────────────────────────────────────────────────────────
    @mcp.tool(
        name="grocy_health",
        description=(
            "Check connectivity to Grocy and report its version. Call this first "
            "when something seems off — a clear `grocy_unreachable` here means the "
            "URL or API key is misconfigured, so you don't discover it on a write."
        ),
    )
    async def health() -> dict[str, Any]:
        # `api_base` already resolves to the instance's `/api` root (whether or
        # not the configured base included it), so the system-info route is a
        # single probe of `{api_base}/system/info` — no double-/api fallback.
        used_path = "/system/info"
        probe = await _probe(used_path)
        if "connect_error" in probe:
            return _GrocyError(
                "grocy_unreachable",
                f"Could not reach Grocy ({probe['connect_error']}).",
                "Check HOMELAB_MCP_GROCY_BASE_URL and that the instance is up.",
            ).payload()
        version = _extract_grocy_version(probe.get("json"))

        log.info("grocy health %s -> status=%s version=%s", used_path, probe.get("status"), version)
        result: dict[str, Any] = {
            "ok": True,
            "grocy_version": version,
            "notes": f"Connected to Grocy {version}." if version else "Connected to Grocy.",
        }
        if version is None:
            # Surface the RAW path + status + body so a misbehaving system-info
            # route (redirect, empty body, non-JSON, unknown shape) is visible in
            # the tool output — the fetch layer, not just the key shape.
            log.warning(
                "grocy health: no version from %s (status=%s content_type=%s body=%r)",
                used_path,
                probe.get("status"),
                probe.get("content_type"),
                probe.get("body_excerpt"),
            )
            result["raw_system_info"] = {
                "path": used_path,
                **{k: probe.get(k) for k in ("status", "content_type", "body_excerpt", "json")},
            }
            result["notes"] += (
                " Could not read a version from system-info — see raw_system_info "
                "(path + HTTP status + body)."
            )
        return result

    @mcp.tool(
        name="grocy_seed_defaults",
        description=(
            "One-shot, idempotent bootstrap for a blank Grocy instance: creates "
            "the default storage locations (Chest Freezer, Kitchen Fridge, Garage "
            "Fridge, Pantry) and quantity units (count, lb, oz, pack). Safe to "
            "re-run — already-present items are reported as not created. Run once "
            "before the first freezer walkthrough."
        ),
    )
    async def seed_defaults() -> dict[str, Any]:
        try:
            locations = [
                await _ensure("locations", name, {"name": name, "description": ""})
                for name in _SEED_LOCATIONS
            ]
            units = [
                await _ensure("quantity_units", name, {"name": name, "name_plural": name})
                for name in _SEED_UNITS
            ]
        except _GrocyError as exc:
            return exc.payload()
        created = sum(1 for x in (*locations, *units) if x["created"])
        existing = len(locations) + len(units) - created
        return {
            "locations": locations,
            "units": units,
            "created_count": created,
            "existing_count": existing,
            "notes": f"Seed complete: {created} created, {existing} already present.",
        }

    async def _ensure_store(
        name: str, address: str | None, description: str | None
    ) -> dict[str, Any]:
        """Idempotent store (shopping-location) upsert with separate address/desc.

        Address lives in a dedicated userfield; description in the store's own
        column. A provided value backfills/updates; a null never clobbers.
        """
        name_s = name.strip()
        addr = address.strip() if address and address.strip() else None
        desc = description.strip() if description and description.strip() else None
        rows = await _list_all(_SHOPPING_LOCATIONS)
        existing = next((r for r in rows if _norm(r.get("name")) == _norm(name_s)), None)
        store_name: str | None
        current_desc: Any
        if existing is None:
            body: dict[str, Any] = {"name": name_s}
            if desc is not None:
                body["description"] = desc
            res = await _call("POST", f"/objects/{_SHOPPING_LOCATIONS}", json=body)
            new_id = res.get("created_object_id") if isinstance(res, dict) else None
            if new_id is None:
                raise _GrocyError("missing_fk", "Grocy did not return a new store id.", "")
            sid, store_name, created = int(new_id), name_s, True
            current_desc = desc
        else:
            sid, store_name, created = int(existing["id"]), existing.get("name"), False
            current_desc = existing.get("description")

        updated = False
        if existing is not None and desc is not None and desc != (current_desc or ""):
            await _call("PUT", f"/objects/{_SHOPPING_LOCATIONS}/{sid}", json={"description": desc})
            current_desc, updated = desc, True

        current_addr = (await _userfield_values(_SHOPPING_LOCATIONS, sid)).get(
            _STORE_ADDRESS_FIELD
        ) or None
        if addr is not None and addr != (current_addr or ""):
            await _ensure_userfield(_SHOPPING_LOCATIONS, _STORE_ADDRESS_FIELD, "Address")
            await _set_userfield(_SHOPPING_LOCATIONS, sid, _STORE_ADDRESS_FIELD, addr)
            current_addr, updated = addr, True

        return {
            "id": sid,
            "name": store_name,
            "address": current_addr,
            "description": current_desc or None,
            "created": created,
            "updated": updated,
        }

    @mcp.tool(
        name="grocy_ensure",
        description=(
            "Idempotently ensure a piece of master data exists, dispatched by "
            "`kind`:\n"
            "  • 'location' → a storage location (e.g. 'Chest Freezer'); optional "
            "`description`. Returns {id, name, created}.\n"
            "  • 'unit' → a quantity unit (e.g. 'count', 'lb', 'pack'); "
            "`name_plural` defaults to the singular. Returns {id, name, created}.\n"
            "  • 'store' → a store / shopping location (e.g. 'Costco'). Optional "
            "`address` (stored in a DEDICATED 'address' userfield, auto-defined on "
            "first use) and `description` (the store's own column) are kept "
            "separate. Upsert: a provided address/description backfills/updates "
            "(updated=true); a null never clobbers. Returns {id, name, address, "
            "description, created, updated}.\n"
            "All kinds match case-insensitively by exact name and create only if "
            "absent. Use before stocking into a location/unit that may not exist "
            "yet; `grocy_stock_item` auto-creates a store on a purchase, so "
            "kind='store' is optional — use it to add/correct an address or note."
        ),
    )
    async def ensure(
        name: Annotated[str, Field(min_length=1, description="Object name to ensure.")],
        kind: Annotated[
            str, Field(description="'location' | 'unit' | 'store'.")
        ] = "location",
        description: Annotated[
            str | None, Field(default=None, description="location/store: optional description.")
        ] = None,
        name_plural: Annotated[
            str | None, Field(default=None, description="unit: plural form; defaults to name.")
        ] = None,
        address: Annotated[
            str | None, Field(default=None, description="store: free-text address (userfield).")
        ] = None,
    ) -> dict[str, Any]:
        if kind not in ("location", "unit", "store"):
            return _GrocyError(
                "kind_invalid", f"Unknown kind '{kind}'.", "Use 'location', 'unit', or 'store'."
            ).payload()
        try:
            if kind == "location":
                return await _ensure(
                    "locations",
                    name.strip(),
                    {"name": name.strip(), "description": description or ""},
                )
            if kind == "unit":
                plural = (name_plural or "").strip() or name.strip()
                return await _ensure(
                    "quantity_units", name.strip(), {"name": name.strip(), "name_plural": plural}
                )
            return await _ensure_store(name, address, description)
        except _GrocyError as exc:
            return exc.payload()

    # ─────────────────────────────────────────────────────────────────
    # Reads
    # ─────────────────────────────────────────────────────────────────
    @mcp.tool(
        name="grocy_find_products",
        description=(
            "Find products by name (case-insensitive substring) across ALL master "
            "products — not just what's in stock. Use to answer 'do we have X?' and "
            "to resolve a spoken name to a product id before stocking. Each hit is "
            "enriched with current amount on hand, stock unit, default location, and "
            "next due date."
        ),
    )
    async def find_products(
        query: Annotated[str, Field(min_length=1, description="Name fragment to search for.")],
        limit: Annotated[int, Field(ge=1, le=100, description="Max results.")] = 20,
    ) -> dict[str, Any]:
        try:
            matches = await _search("products", query.strip())
            enriched = await _map_bounded(_enrich, matches[:limit])
        except _GrocyError as exc:
            return exc.payload()
        total = len(matches)
        return {
            "count": len(enriched),
            "returned": len(enriched),
            "total": total,
            "truncated": total > len(enriched),
            "products": enriched,
        }

    @mcp.tool(
        name="grocy_attention",
        description=(
            "The 'what needs attention?' feed, summarized (not raw stock rows). "
            "`kind='expiring'` (default) is the DATE-driven meal-planning view — "
            "products due soon, overdue, or expired within `days` — for 'what's "
            "going bad?' / 'what should I use up?'. `kind='below_minimum'` is the "
            "QUANTITY-driven restock signal — products under their configured "
            "minimum stock, with shortfall and default location, for a buy list. "
            "Each item is projected to {product_id, name, amount/on_hand, due/"
            "shortfall, bucket}. Read-only."
        ),
    )
    async def attention(
        kind: Annotated[
            str, Field(description="'expiring' (date-driven) | 'below_minimum' (quantity-driven).")
        ] = "expiring",
        days: Annotated[
            int, Field(ge=0, le=365, description="'Due soon' horizon (kind='expiring').")
        ] = 5,
    ) -> dict[str, Any]:
        if kind not in ("expiring", "below_minimum"):
            return _GrocyError(
                "kind_invalid", f"Unknown kind '{kind}'.", "Use 'expiring' or 'below_minimum'."
            ).payload()
        try:
            params = {"due_soon_days": days} if kind == "expiring" else None
            vol = await _call("GET", "/stock/volatile", params=params)
            vol = vol if isinstance(vol, dict) else {}

            if kind == "expiring":
                today = datetime.now(UTC).date()

                def _proj(entry: dict[str, Any], bucket: str) -> dict[str, Any]:
                    raw_prod = entry.get("product")
                    prod = raw_prod if isinstance(raw_prod, dict) else {}
                    pid = prod.get("id") or entry.get("product_id") or entry.get("id")
                    name = prod.get("name") or entry.get("name")
                    due = entry.get("best_before_date") or entry.get("next_due_date")
                    days_until: int | None = None
                    if due:
                        try:
                            days_until = (date.fromisoformat(str(due)[:10]) - today).days
                        except ValueError:
                            days_until = None
                    return {
                        "product_id": pid,
                        "name": name,
                        "amount": entry.get("amount"),
                        "due_date": due,
                        "days_until_due": days_until,
                        "bucket": bucket,
                    }

                items = [
                    _proj(e, bucket)
                    for key, bucket in (
                        ("due_products", "due_soon"),
                        ("overdue_products", "overdue"),
                        ("expired_products", "expired"),
                    )
                    for e in (vol.get(key) or [])
                ]
            else:

                async def _below(m: dict[str, Any]) -> dict[str, Any]:
                    pid = m.get("id")
                    on_hand: Any = None
                    min_stock: Any = None
                    location: str | None = None
                    if pid is not None:
                        try:
                            master = await _call("GET", f"/objects/products/{pid}")
                            if isinstance(master, dict):
                                min_stock = master.get("min_stock_amount")
                        except _GrocyError:
                            pass
                        detail = await _stock_detail(int(pid))
                        if detail:
                            on_hand = detail.get("stock_amount")
                            loc = detail.get("location") or detail.get("default_location") or {}
                            location = loc.get("name")
                    return {
                        "product_id": pid,
                        "name": m.get("name"),
                        "on_hand": on_hand,
                        "min_stock": min_stock,
                        "shortfall": m.get("amount_missing"),
                        "default_location": location,
                        "bucket": "below_minimum",
                    }

                items = await _map_bounded(_below, list(vol.get("missing_products") or []))
        except _GrocyError as exc:
            return exc.payload()

        note = (
            f"{len(items)} product(s) due within {days}d / overdue / expired."
            if kind == "expiring"
            else f"{len(items)} product(s) below minimum."
        )
        return {
            "kind": kind,
            "days": days,
            "returned": len(items),
            "total": len(items),
            "truncated": False,
            "items": items,
            "notes": note,
        }

    # ─────────────────────────────────────────────────────────────────
    # The keystone walkthrough tool
    # ─────────────────────────────────────────────────────────────────
    @mcp.tool(
        name="grocy_stock_item",
        description=(
            "Book a single item in or out during a freezer/fridge/pantry "
            "walkthrough: find-or-create the product, then apply a stock action — "
            "all in one call.\n\n"
            "ACTIONS:\n"
            "  • set     → reconcile to an ABSOLUTE amount (inventory). Use for a "
            "walkthrough: 'there are two racks' means the truth is 2. Grocy "
            "reconciles the product's total on hand; new stock lands at `location`. "
            "The result reports previous_amount_on_hand so a surprising "
            "reconciliation is visible.\n"
            "  • add     → append stock (a restock/purchase). Prefer this when "
            "items carry DISTINCT best-before dates you want tracked separately — "
            "each add is its own dated entry.\n"
            "  • consume → remove stock (oldest-due first). Set `spoiled=true` to "
            "record waste rather than use.\n"
            "  • open    → mark an amount opened (can shift the applicable due "
            "date).\n\n"
            "IDENTIFICATION: pass a `name` (resolved find-or-create), a known "
            "`product_id`, or a `barcode` (consume/open only). NAME RESOLUTION "
            "never guesses: exact case-insensitive match → use it. No match at all "
            "+ create_if_missing → create it (created=true). A NEAR match but no "
            "exact one → returns needs_disambiguation with candidates; confirm, "
            "then re-call with `product_id` — OR pass create_new=true to force a "
            "brand-new product with that exact name instead of disambiguating.\n\n"
            "Creating a product requires `location` and `unit` to already exist — "
            "if they don't, you get location_not_found / unit_not_found naming the "
            "missing one (run grocy_ensure first; this tool never invents master "
            "scaffolding). For meat, always pass a real `best_before` date.\n\n"
            "PRICE (action='add' only): pass `total_price` (for the whole `amount`) "
            "OR `unit_price` (per stock unit) — not both (price_ambiguous). Grocy "
            "stores price per stock unit, so for variable-weight items stock by "
            "weight (unit='lb') and pass total_price so $/lb is real. Tag the "
            "purchase with `store` (e.g. 'Costco', auto-created) and an optional "
            "`purchased_date`. These light up grocy_stock_value and product_card "
            "price history. Price <= 0 → price_invalid; price on set/consume is "
            "rejected."
        ),
    )
    async def stock_item(
        amount: Annotated[float, Field(description="Amount > 0, in the product's stock unit.")],
        location: Annotated[
            str | int | None,
            Field(default=None, description="Location name or id (required for set/add)."),
        ] = None,
        name: Annotated[
            str | None,
            Field(
                default=None, description="Spoken product name, e.g. 'ribeye' (or pass product_id)."
            ),
        ] = None,
        action: Annotated[str, Field(description="'set' | 'add' | 'consume' | 'open'.")] = "set",
        unit: Annotated[
            str | int, Field(description="Unit (name or id); used only when creating.")
        ] = "count",
        best_before: Annotated[
            str | None, Field(default=None, description="YYYY-MM-DD; null = product default/today.")
        ] = None,
        spoiled: Annotated[bool, Field(description="Only for action='consume'.")] = False,
        create_if_missing: Annotated[
            bool, Field(description="Create the product if no match.")
        ] = True,
        create_new: Annotated[
            bool,
            Field(
                description="Force-create a NEW product with this exact name even if "
                "near-matches exist (skips disambiguation).",
            ),
        ] = False,
        product_id: Annotated[
            int | None, Field(default=None, ge=1, description="Bypass name resolution if known.")
        ] = None,
        barcode: Annotated[
            str | None,
            Field(default=None, description="Identify by barcode (consume/open only)."),
        ] = None,
        total_price: Annotated[
            float | None,
            Field(default=None, description="action='add' only: price for the WHOLE amount."),
        ] = None,
        unit_price: Annotated[
            float | None,
            Field(default=None, description="action='add' only: price per ONE stock unit."),
        ] = None,
        store: Annotated[
            str | None,
            Field(
                default=None, description="action='add' only: shopping location (e.g. 'Costco')."
            ),
        ] = None,
        purchased_date: Annotated[
            str | None,
            Field(default=None, description="action='add' only: YYYY-MM-DD; default today."),
        ] = None,
    ) -> dict[str, Any]:
        if action not in ("set", "add", "consume", "open"):
            return _GrocyError(
                "action_invalid", f"Unknown action '{action}'.", "Use set, add, consume, or open."
            ).payload()
        if amount is None or amount <= 0:
            return _GrocyError("amount_invalid", "Amount must be greater than zero.", "").payload()

        # ── barcode identification (absorbs the old consume/open primitives) ──
        if barcode is not None and str(barcode).strip():
            bc = str(barcode).strip()
            if action not in ("consume", "open"):
                return _GrocyError(
                    "identifier_invalid",
                    "Barcode identification supports only consume/open.",
                    "Use product_id or name for set/add.",
                ).payload()
            if product_id is not None or (name and name.strip()):
                return _GrocyError(
                    "identifier_invalid",
                    "Provide a barcode OR a name/product_id, not both.",
                    "",
                ).payload()
            path = f"/stock/products/by-barcode/{_enc(bc)}/{action}"
            bc_body = (
                {"amount": amount, "transaction_type": "consume", "spoiled": spoiled}
                if action == "consume"
                else {"amount": amount}
            )
            try:
                data = await _call("POST", path, json=bc_body)
            except _GrocyError as exc:
                return exc.payload()
            did = "consumed" if action == "consume" else "opened"
            return {
                "action_taken": action,
                "product": {"barcode": bc},
                "amount": amount,
                "resulting_amount_on_hand": None,
                "stock_log": data,
                "notes": f"{did} {amount} via barcode"
                + (" (spoiled)." if (action == "consume" and spoiled) else "."),
            }

        if product_id is None and not (name and name.strip()):
            return _GrocyError(
                "product_not_found",
                "No product name, id, or barcode given.",
                "Pass name, product_id, or barcode.",
            ).payload()
        if action in ("set", "add") and (location is None or str(location).strip() == ""):
            return _GrocyError(
                "location_not_found",
                "A location is required for set/add.",
                "Pass a location name or id.",
            ).payload()

        # Price contract (action='add' only). Grocy stores price PER STOCK UNIT.
        eff_unit_price: float | None = None
        if total_price is not None or unit_price is not None:
            if action != "add":
                return _GrocyError(
                    "price_invalid",
                    "Price applies only to action='add' (a purchase).",
                    "Use action='add' to record a price.",
                ).payload()
            if total_price is not None and unit_price is not None:
                return _GrocyError(
                    "price_ambiguous",
                    "Pass only one of total_price or unit_price.",
                    "total_price is for the whole amount; unit_price is per stock unit.",
                ).payload()
            if unit_price is not None:
                if unit_price <= 0:
                    return _GrocyError(
                        "price_invalid", "unit_price must be greater than zero.", ""
                    ).payload()
                eff_unit_price = unit_price
            else:
                assert total_price is not None
                if total_price <= 0:
                    return _GrocyError(
                        "price_invalid", "total_price must be greater than zero.", ""
                    ).payload()
                eff_unit_price = total_price / amount

        try:
            # Location is needed for set/add and for a create path; for a
            # consume/open by id/name it is optional (omitted from the body).
            location_id: int | None = None
            if location is not None and str(location).strip() != "":
                location_id = await _resolve_id("locations", location, "location_not_found")

            # ── resolve product ──────────────────────────────────────
            product: dict[str, Any]
            created = False
            if product_id is not None:
                try:
                    found = await _call("GET", f"/objects/products/{product_id}")
                except _GrocyError as exc:
                    raise _GrocyError(
                        "product_not_found", f"No product with id {product_id}.", "Check the id."
                    ) from exc
                if not isinstance(found, dict) or not found.get("id"):
                    raise _GrocyError(
                        "product_not_found", f"No product with id {product_id}.", "Check the id."
                    )
                product = {"id": int(found["id"]), "name": found.get("name"), "created": False}
            else:
                assert name is not None  # guaranteed by the guard above
                candidates = await _search("products", name.strip())
                exact = [c for c in candidates if _norm(c.get("name")) == _norm(name)]
                if len(exact) == 1:
                    product = {
                        "id": int(exact[0]["id"]),
                        "name": exact[0].get("name"),
                        "created": False,
                    }
                elif create_new:
                    # Escape hatch: force a brand-new product with this exact
                    # name instead of disambiguating against near-matches.
                    if location_id is None:
                        raise _GrocyError(
                            "location_not_found",
                            "A location is required to create a product.",
                            "Pass a location name or id.",
                        )
                    unit_id = await _resolve_id("quantity_units", unit, "unit_not_found")
                    product = await _create_product(name, location_id, unit_id)
                    created = True
                elif len(exact) > 1 or candidates:
                    cands = await _map_bounded(_enrich, candidates[:10])
                    return {
                        **_GrocyError(
                            "needs_disambiguation",
                            f"'{name}' matches existing products; not guessing.",
                            "Re-call with product_id set to the intended one, "
                            "or pass create_new=true to make a new product.",
                        ).payload(),
                        "candidates": cands,
                    }
                else:
                    if not create_if_missing:
                        return _GrocyError(
                            "product_not_found",
                            f"No product named '{name}'.",
                            "Set create_if_missing=true to create it.",
                        ).payload()
                    # Re-check by exact name right before create (race guard).
                    recheck = [
                        c
                        for c in await _search("products", name.strip())
                        if _norm(c.get("name")) == _norm(name)
                    ]
                    if len(recheck) == 1:
                        product = {
                            "id": int(recheck[0]["id"]),
                            "name": recheck[0].get("name"),
                            "created": False,
                        }
                    else:
                        if location_id is None:
                            raise _GrocyError(
                                "location_not_found",
                                "A location is required to create a product.",
                                "Pass a location name or id.",
                            )
                        unit_id = await _resolve_id("quantity_units", unit, "unit_not_found")
                        product = await _create_product(name, location_id, unit_id)
                        created = True

            pid = int(product["id"])

            # Store (shopping location) tags a purchase. Low-cardinality and
            # append-only, so create-on-miss is OK here (unlike products).
            store_id: int | None = None
            store_name: str | None = None
            store_created = False
            if action == "add" and store is not None and str(store).strip() != "":
                store_info = await _ensure(
                    "shopping_locations", str(store).strip(), {"name": str(store).strip()}
                )
                if store_info.get("id") is None:
                    raise _GrocyError("store_not_found", f"Could not resolve store '{store}'.", "")
                store_id = int(store_info["id"])
                store_name = store_info.get("name")
                store_created = bool(store_info.get("created"))

            # ── apply the action ─────────────────────────────────────
            previous: float | None = None
            if action == "set":
                # Grocy's inventory endpoint sets the product-wide total; read
                # what it will reconcile FROM so a surprising set is visible.
                previous = await _amount_on_hand(pid)
                body = _compact(
                    {
                        "new_amount": amount,
                        "best_before_date": best_before,
                        "location_id": location_id,
                    }
                )
                await _call("POST", f"/stock/products/{pid}/inventory", json=body)
            elif action == "add":
                body = _compact(
                    {
                        "amount": amount,
                        "transaction_type": "purchase",
                        "best_before_date": best_before,
                        "location_id": location_id,
                        "price": eff_unit_price,
                        "purchased_date": purchased_date,
                        "shopping_location_id": store_id,
                    }
                )
                await _call("POST", f"/stock/products/{pid}/add", json=body)
            elif action == "open":
                await _call("POST", f"/stock/products/{pid}/open", json={"amount": amount})
            else:  # consume
                body = _compact(
                    {
                        "amount": amount,
                        "transaction_type": "consume",
                        "spoiled": spoiled,
                        "location_id": location_id,
                    }
                )
                await _call("POST", f"/stock/products/{pid}/consume", json=body)

            resulting = await _amount_on_hand(pid)
        except _GrocyError as exc:
            return exc.payload()

        pname = product.get("name") or (name or "").strip() or f"product {pid}"
        if action == "set":
            verb = f"set {pname} at {location} to {amount}"
        elif action == "add":
            verb = f"added {amount} {pname} at {location}"
            if eff_unit_price is not None:
                verb += f" @ {round(eff_unit_price, 4)}/unit"
            if store_name:
                verb += f" from {store_name}" + (" (new store)" if store_created else "")
        elif action == "open":
            verb = f"opened {amount} {pname}"
        else:
            verb = f"consumed {amount} {pname}" + (" (spoiled)" if spoiled else "")
        on_hand = "unknown" if resulting is None else resulting
        prefix = "Created and " if created else ""
        result: dict[str, Any] = {
            "action_taken": action,
            "product": {"id": pid, "name": pname, "created": created},
            "location": location,
            "amount": amount,
            "best_before": best_before,
            "resulting_amount_on_hand": resulting,
            "notes": f"{prefix}{verb}; {on_hand} on hand.",
        }
        if action == "set":
            result["previous_amount_on_hand"] = previous
        if action == "add":
            result["unit_price"] = round(eff_unit_price, 4) if eff_unit_price is not None else None
            result["total_price"] = (
                round(eff_unit_price * amount, 2) if eff_unit_price is not None else None
            )
            result["store"] = store_name
            result["purchased_date"] = purchased_date
        return result

    # ─────────────────────────────────────────────────────────────────
    # Enrichment reads (read-only; surface what Grocy's engine computes)
    # ─────────────────────────────────────────────────────────────────
    @mcp.tool(
        name="grocy_convert_units",
        description=(
            "Convert an amount of a product between quantity units using Grocy's "
            "conversion engine — the building block for netting recipe quantities "
            "against on-hand stock ('recipe needs 2 lb' vs 'we have 1 pack'). "
            "Resolution order: product-specific conversion → global conversion → "
            "identity (same unit). `conversion_source` reports which was used. If "
            "no conversion exists, returns `no_conversion_path` (never a silent "
            "1:1). Read-only. Resolve the product by name (exact match) or id; a "
            "near-but-inexact name returns needs_disambiguation. On "
            "no_conversion_path the payload includes the product's defined "
            "conversion rows (global + product-specific) for inspection."
        ),
    )
    async def convert_units(
        product: Annotated[str | int, Field(description="Product name or id.")],
        amount: Annotated[float, Field(description="Amount in from_unit (> 0).")],
        from_unit: Annotated[str | int, Field(description="Source unit name or id.")],
        to_unit: Annotated[str | int, Field(description="Target unit name or id.")],
    ) -> dict[str, Any]:
        if amount is None or amount <= 0:
            return _GrocyError("amount_invalid", "Amount must be greater than zero.", "").payload()
        try:
            prod = await _resolve_product(product)
            from_id, from_name = await _resolve_unit(from_unit)
            to_id, to_name = await _resolve_unit(to_unit)
            if from_id == to_id:
                factor, source = 1.0, "identity"
            else:
                conv = await _find_conversion(int(prod["id"]), from_id, to_id)
                if conv is None:
                    return {
                        **_GrocyError(
                            "no_conversion_path",
                            f"No conversion from {from_name or from_unit} to "
                            f"{to_name or to_unit} for {prod['name']}.",
                            "Add a quantity-unit conversion in Grocy first.",
                        ).payload(),
                        "conversions": await _conversion_rows(int(prod["id"])),
                    }
                factor, source = conv
        except _Disambiguation as dis:
            return dis.payload()
        except _GrocyError as exc:
            return exc.payload()
        amount_out = round(amount * factor, 4)
        fname, tname = from_name or str(from_unit), to_name or str(to_unit)
        return {
            "product": prod,
            "amount_in": amount,
            "from_unit": fname,
            "amount_out": amount_out,
            "to_unit": tname,
            "conversion_source": source,
            "factor": factor,
            "notes": f"{amount} {fname} = {amount_out} {tname} ({source}).",
        }

    @mcp.tool(
        name="grocy_product_card",
        description=(
            "The full enriched picture behind a product — what Grocy's product/"
            "stock detail view shows: on-hand (and opened) amounts, minimum-stock "
            "and a below_minimum flag, next due date, average shelf life, last/"
            "average price, default location + product group, and a per-location "
            "amount breakdown. Read-only. Resolve by name (exact) or id."
        ),
    )
    async def product_card(
        product: Annotated[str | int, Field(description="Product name or id.")],
    ) -> dict[str, Any]:
        try:
            prod = await _resolve_product(product)
            pid = int(prod["id"])

            async def _locs() -> Any:
                try:
                    return await _call("GET", f"/stock/products/{pid}/locations")
                except _GrocyError:
                    return []

            # The three detail reads are independent — fetch them concurrently.
            detail, master, locs = await asyncio.gather(
                _stock_detail(pid),
                _call("GET", f"/objects/products/{pid}"),
                _locs(),
            )
        except _Disambiguation as dis:
            return dis.payload()
        except _GrocyError as exc:
            return exc.payload()

        detail = detail or {}
        master = master if isinstance(master, dict) else {}
        qu = detail.get("quantity_unit_stock") or {}
        loc = detail.get("location") or detail.get("default_location") or {}
        on_hand = detail.get("stock_amount")
        min_stock = master.get("min_stock_amount")
        below: bool | None = None
        if on_hand is not None and min_stock not in (None, ""):
            try:
                below = float(str(on_hand)) < float(str(min_stock))
            except (TypeError, ValueError):
                below = None

        group_name: str | None = None
        gid = master.get("product_group_id")
        if gid:
            try:
                grp = await _call("GET", f"/objects/product_groups/{gid}")
                group_name = grp.get("name") if isinstance(grp, dict) else None
            except _GrocyError:
                group_name = None

        locations = [
            {"location": x.get("location_name"), "amount": x.get("amount")}
            for x in (locs if isinstance(locs, list) else [])
        ]
        oh = "unknown" if on_hand is None else on_hand
        return {
            "id": pid,
            "name": prod["name"],
            "default_location": loc.get("name"),
            "product_group": group_name,
            "stock_unit": qu.get("name"),
            "on_hand": on_hand,
            "on_hand_opened": detail.get("stock_amount_opened"),
            "min_stock_amount": min_stock,
            "below_minimum": below,
            "next_due_date": detail.get("next_due_date"),
            "avg_shelf_life_days": detail.get("average_shelf_life_days"),
            "last_purchased_date": detail.get("last_purchased"),
            "last_price": detail.get("last_price"),
            "avg_price": detail.get("avg_price"),
            "locations": locations,
            "notes": f"{prod['name']}: {oh} {qu.get('name') or ''} on hand"
            + (" (below minimum)" if below else "")
            + ".",
        }

    @mcp.tool(
        name="grocy_consumption_history",
        description=(
            "Burn rate for a product — 'how fast do we go through X' — derived from "
            "Grocy's stock transaction log over a lookback window. Returns "
            "purchased/consumed/spoiled totals and per-week / per-month consume "
            "rates so shopping can be predictive. Read-only; summarized, not raw "
            "rows. Resolve by name (exact) or id."
        ),
    )
    async def consumption_history(
        product: Annotated[str | int, Field(description="Product name or id.")],
        days: Annotated[int, Field(ge=1, le=730, description="Lookback window in days.")] = 90,
    ) -> dict[str, Any]:
        try:
            prod = await _resolve_product(product)
            pid = int(prod["id"])
            rows = await _call(
                "GET",
                "/objects/stock_log",
                params={
                    "query[]": f"product_id={pid}",
                    "order": "row_created_timestamp:desc",
                    "limit": _MAX_LOG_ROWS,
                },
            )
        except _Disambiguation as dis:
            return dis.payload()
        except _GrocyError as exc:
            return exc.payload()

        rows = rows if isinstance(rows, list) else []
        cutoff = (datetime.now(UTC) - timedelta(days=days)).date().isoformat()
        purchased = consumed = spoiled = 0.0
        last_purchased: str | None = None
        last_consumed: str | None = None
        count = 0
        for r in rows:
            if r.get("undone"):
                continue
            ts = str(r.get("row_created_timestamp") or "")[:10]
            if ts and ts < cutoff:
                continue
            count += 1
            amt = abs(float(r.get("amount") or 0))
            ttype = r.get("transaction_type")
            if ttype == "purchase":
                purchased += amt
                pd = r.get("purchased_date") or ts
                if pd and (last_purchased is None or pd > last_purchased):
                    last_purchased = pd
            elif ttype == "consume":
                if r.get("spoiled"):
                    spoiled += amt
                else:
                    consumed += amt
                ud = r.get("used_date") or ts
                if ud and (last_consumed is None or ud > last_consumed):
                    last_consumed = ud
        per_week = round(consumed / (days / 7), 2) if days else 0.0
        per_month = round(consumed / (days / 30), 2) if days else 0.0
        # The stock-log read is capped at _MAX_LOG_ROWS. When that cap is hit the
        # window may extend past the oldest row we fetched, so totals/rates can
        # undercount — flag it rather than silently dropping data.
        truncated = len(rows) >= _MAX_LOG_ROWS
        note = (
            f"Over {days}d: consumed {round(consumed, 2)} ({per_week}/wk), "
            f"{round(spoiled, 2)} spoiled."
        )
        if truncated:
            note += (
                f" NOTE: stock log capped at {_MAX_LOG_ROWS} rows — totals/rates "
                "may undercount."
            )
        return {
            "product": prod,
            "window_days": days,
            "purchased_total": round(purchased, 2),
            "consumed_total": round(consumed, 2),
            "spoiled_total": round(spoiled, 2),
            "consume_rate_per_week": per_week,
            "consume_rate_per_month": per_month,
            "last_purchased_date": last_purchased,
            "last_consumed_date": last_consumed,
            "transactions_count": count,
            "returned": len(rows),
            "truncated": truncated,
            "notes": note,
        }

    @mcp.tool(
        name="grocy_stock_value",
        description=(
            "What the inventory is worth: total value of current stock in Grocy's "
            "configured currency, optionally broken down by location, plus a top-N "
            "by product. Use to put a dollar figure on the freezer or cost out a "
            "shopping list. Read-only. Entries without a recorded price are noted "
            "and excluded from the total."
        ),
    )
    async def stock_value(
        by_location: Annotated[
            bool, Field(description="Include a per-location breakdown.")
        ] = False,
    ) -> dict[str, Any]:
        try:
            entries = await _call("GET", "/objects/stock")
            currency = await _currency()
            loc_names = await _name_map("locations")
            prod_names = await _name_map("products")
        except _GrocyError as exc:
            return exc.payload()

        entries = entries if isinstance(entries, list) else []
        total = 0.0
        by_loc: dict[int, float] = {}
        by_prod: dict[int, float] = {}
        unpriced = 0
        for e in entries:
            price = e.get("price")
            if price in (None, ""):
                unpriced += 1
                continue
            val = float(e.get("amount") or 0) * float(price)
            total += val
            lid = e.get("location_id")
            if lid is not None:
                by_loc[int(lid)] = by_loc.get(int(lid), 0.0) + val
            pid = e.get("product_id")
            if pid is not None:
                by_prod[int(pid)] = by_prod.get(int(pid), 0.0) + val

        out: dict[str, Any] = {"total_value": round(total, 2), "currency": currency}
        if by_location:
            out["by_location"] = [
                {"location": loc_names.get(k, str(k)), "value": round(v, 2)}
                for k, v in sorted(by_loc.items(), key=lambda kv: -kv[1])
            ]
        out["by_product_top"] = [
            {"product": prod_names.get(k, str(k)), "value": round(v, 2)}
            for k, v in sorted(by_prod.items(), key=lambda kv: -kv[1])[:10]
        ]
        note = f"Total stock value {round(total, 2)}" + (f" {currency}" if currency else "") + "."
        if unpriced:
            note += f" {unpriced} entr{'y' if unpriced == 1 else 'ies'} had no price."
        out["notes"] = note
        return out

    @mcp.tool(
        name="grocy_stock_by_location",
        description=(
            "On-hand stock grouped by storage location — 'what's in the chest "
            "freezer'. Pass a `location` (name or id) to scope to one, or omit for "
            "all (the full 'what do we have?' view). Each product shows id, amount, "
            "stock unit, and next due date. Read-only."
        ),
    )
    async def stock_by_location(
        location: Annotated[
            str | int | None,
            Field(default=None, description="Location name or id; omit for all."),
        ] = None,
    ) -> dict[str, Any]:
        try:
            loc_filter: int | None = None
            if location is not None and str(location).strip() != "":
                loc_filter = await _resolve_id("locations", location, "location_not_found")
            # Four independent collection reads — fetch them concurrently.
            entries, products, unit_names, loc_names = await asyncio.gather(
                _call("GET", "/objects/stock"),
                _call("GET", "/objects/products"),
                _name_map("quantity_units"),
                _name_map("locations"),
            )
        except _GrocyError as exc:
            return exc.payload()

        entries = entries if isinstance(entries, list) else []
        pmaster = {
            int(p["id"]): p
            for p in (products if isinstance(products, list) else [])
            if p.get("id") is not None
        }
        groups: dict[int, list[dict[str, Any]]] = {}
        for e in entries:
            lid = e.get("location_id")
            if lid is None:
                continue
            lid = int(lid)
            if loc_filter is not None and lid != loc_filter:
                continue
            pid = e.get("product_id")
            prod = pmaster.get(int(pid)) if pid is not None else None
            unit = None
            if prod and prod.get("qu_id_stock") is not None:
                unit = unit_names.get(int(prod["qu_id_stock"]))
            groups.setdefault(lid, []).append(
                {
                    "id": pid,
                    "name": prod.get("name") if prod else None,
                    "amount": e.get("amount"),
                    "stock_unit": unit,
                    "next_due_date": e.get("best_before_date"),
                }
            )

        locations = [
            {"location": loc_names.get(k, str(k)), "products": v} for k, v in groups.items()
        ]
        n_items = sum(len(v) for v in groups.values())
        return {
            "returned": n_items,
            "total": n_items,
            "truncated": False,
            "locations": locations,
            "notes": f"{n_items} stock entr{'y' if n_items == 1 else 'ies'} "
            f"across {len(locations)} location(s).",
        }

    # ─────────────────────────────────────────────────────────────────
    # Unit-conversion authoring (the only master-data write beyond product
    # create — upsert only, no delete)
    # ─────────────────────────────────────────────────────────────────
    @mcp.tool(
        name="grocy_set_unit_conversion",
        description=(
            "Add or update a quantity-unit conversion so `grocy_convert_units` can "
            "use it — e.g. teach Grocy 'a pack of ribeye is 4 lb' during a "
            "walkthrough. `factor` is the amount of `to_unit` per 1 `from_unit` "
            "(pack→lb factor 4 means 1 pack = 4 lb). Pass `product` for a "
            "product-specific conversion, or omit it for a GLOBAL one (e.g. "
            "1 lb = 16 oz everywhere).\n\n"
            "Write ONE direction only — `grocy_convert_units` inverts "
            "automatically, so setting pack→lb=4 makes lb→pack=0.25 resolve on its "
            "own. Do not also write the reverse.\n\n"
            "Idempotent upsert: an existing row for the same (product, from, to) is "
            "updated in place (result='updated'); otherwise a new row is created "
            "(result='created'). Never makes a duplicate. Units and product must "
            "already exist (unit_not_found / product_not_found); a near-but-inexact "
            "product name returns needs_disambiguation."
        ),
    )
    async def set_unit_conversion(
        from_unit: Annotated[str | int, Field(description="Source unit name or id.")],
        to_unit: Annotated[str | int, Field(description="Target unit name or id.")],
        factor: Annotated[float, Field(description="Amount of to_unit per 1 from_unit (> 0).")],
        product: Annotated[
            str | int | None,
            Field(
                default=None,
                description="Product name/id for a specific conversion; omit for global.",
            ),
        ] = None,
    ) -> dict[str, Any]:
        if factor is None or factor <= 0:
            return _GrocyError("factor_invalid", "Factor must be greater than zero.", "").payload()
        try:
            from_id, from_name = await _resolve_unit(from_unit)
            to_id, to_name = await _resolve_unit(to_unit)
            prod: dict[str, Any] | None = None
            if product is not None and str(product).strip() != "":
                prod = await _resolve_product(product)
            pid = int(prod["id"]) if prod else None

            rows = await _query_conversions(from_id, to_id)
            if pid is None:
                match = next((r for r in rows if not r.get(_CONV_PRODUCT)), None)
            else:
                match = next((r for r in rows if str(r.get(_CONV_PRODUCT) or "") == str(pid)), None)

            body: dict[str, Any] = {_CONV_FROM: from_id, _CONV_TO: to_id, _CONV_FACTOR: factor}
            if pid is not None:
                body[_CONV_PRODUCT] = pid

            if match is not None:
                cid: int | None = int(match["id"])
                await _call("PUT", f"/objects/{_CONV_ENTITY}/{cid}", json=body)
                result = "updated"
            else:
                res = await _call("POST", f"/objects/{_CONV_ENTITY}", json=body)
                cid = res.get("created_object_id") if isinstance(res, dict) else None
                result = "created"
        except _Disambiguation as dis:
            return dis.payload()
        except _GrocyError as exc:
            return exc.payload()

        fname, tname = from_name or str(from_unit), to_name or str(to_unit)
        scope = prod["name"] if prod else "global"
        return {
            "product": prod,
            "from_unit": fname,
            "to_unit": tname,
            "factor": factor,
            "result": result,
            "conversion_id": cid,
            "notes": (
                f"{result.capitalize()} {scope} conversion: 1 {fname} = {factor} {tname} "
                "(reverse resolves automatically)."
            ),
        }
