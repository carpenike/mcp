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

import logging
from typing import TYPE_CHECKING, Annotated, Any
from urllib.parse import quote

import httpx
from pydantic import Field

if TYPE_CHECKING:
    from mcp.server.fastmcp import FastMCP

    from homelab_mcp.config import Settings

log = logging.getLogger(__name__)

# Bound on how long we wait for any single Grocy call.
_TIMEOUT = 15

# Idempotent seed for a blank instance (see handoff §7.2). Plurals default
# to the singular — Grocy requires name_plural but our units read naturally
# either way ("2 lb", "2 count").
_SEED_LOCATIONS = ["Chest Freezer", "Kitchen Fridge", "Garage Fridge", "Pantry"]
_SEED_UNITS = ["count", "lb", "oz", "pack"]


class _GrocyError(Exception):
    """An internal failure carrying a stable error `code` for the client.

    Always surfaced as a structured ``{"error": {code, message, hint}}``
    payload — never raised to the MCP transport, and never carrying the
    API key or a raw upstream response.
    """

    def __init__(self, code: str, message: str, hint: str = "") -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.hint = hint

    def payload(self) -> dict[str, Any]:
        return {"error": {"code": self.code, "message": self.message, "hint": self.hint}}


def _enc(segment: str) -> str:
    """URL-encode a single path segment, leaving no `/` to enable traversal."""
    return quote(segment, safe="")


def _compact(body: dict[str, Any]) -> dict[str, Any]:
    """Drop keys whose value is None so we only send fields the caller set."""
    return {k: v for k, v in body.items() if v is not None}


def _norm(value: Any) -> str:
    """Trim + lowercase for case-insensitive name comparison."""
    return str(value or "").strip().lower()


def register(mcp: FastMCP, settings: Settings) -> None:
    """Register grocy_* stock/inventory tools on the given MCP server."""
    base = settings.grocy_base_url.rstrip("/")
    api_key = settings.grocy_api_key

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
        url = f"{base}{path}"
        try:
            async with httpx.AsyncClient(
                timeout=_TIMEOUT,
                headers={"GROCY-API-KEY": api_key},
            ) as client:
                resp = await client.request(method, url, params=params, json=json)
        except httpx.HTTPError as exc:
            # Log class + path only — never the key, query, or body.
            log.warning("grocy %s %s failed: %s", method, path, exc.__class__.__name__)
            raise _GrocyError(
                "grocy_unreachable",
                f"Could not reach Grocy ({exc.__class__.__name__}).",
                "Check HOMELAB_MCP_GROCY_BASE_URL and that the instance is up.",
            ) from exc

        if resp.status_code >= 400:
            detail: str | None = None
            try:
                payload = resp.json()
                if isinstance(payload, dict):
                    detail = payload.get("error_message")
            except ValueError:
                detail = (resp.text or "")[:200] or None
            raise _GrocyError(
                f"grocy_http_{resp.status_code}",
                detail or f"Grocy returned HTTP {resp.status_code}.",
                "",
            )

        if resp.status_code == 204 or not resp.content:
            return None
        try:
            return resp.json()
        except ValueError as exc:
            raise _GrocyError(
                f"grocy_http_{resp.status_code}", "Grocy returned a non-JSON response.", ""
            ) from exc

    # ── master-data helpers ─────────────────────────────────────────
    async def _search(entity: str, name: str) -> list[dict[str, Any]]:
        """LIKE-search an /objects/{entity} collection by name (substring)."""
        data = await _call("GET", f"/objects/{entity}", params={"query[]": f"name~{name}"})
        return data if isinstance(data, list) else []

    async def _resolve_id(entity: str, value: str | int, not_found_code: str) -> int:
        """Resolve a name-or-id to an existing id; raise if a name has no exact match."""
        if isinstance(value, int):
            return value
        s = str(value).strip()
        if s.isdigit():
            return int(s)
        exact = [i for i in await _search(entity, s) if _norm(i.get("name")) == _norm(s)]
        singular = entity.rstrip("s").replace("_", " ")
        if len(exact) == 1:
            return int(exact[0]["id"])
        if len(exact) > 1:
            raise _GrocyError(
                not_found_code, f"Multiple {entity} named '{s}'.", "Pass the numeric id instead."
            )
        raise _GrocyError(
            not_found_code,
            f"No {singular} named '{s}'.",
            "Create it first with the matching ensure_* tool.",
        )

    async def _ensure(entity: str, name: str, create_body: dict[str, Any]) -> dict[str, Any]:
        """Idempotent lookup-or-create on an /objects/{entity} collection."""
        exact = [i for i in await _search(entity, name) if _norm(i.get("name")) == _norm(name)]
        if exact:
            return {"id": int(exact[0]["id"]), "name": exact[0].get("name"), "created": False}
        res = await _call("POST", f"/objects/{entity}", json=create_body)
        new_id = res.get("created_object_id") if isinstance(res, dict) else None
        return {"id": new_id, "name": name, "created": True}

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
        """Create a master product with stock unit == purchase unit (factor 1).

        Minimal body confirmed against grocy/grocy master OpenAPI. See the
        module-level NOTE if a deployed instance rejects these fields.
        """
        body = {
            "name": name.strip(),
            "location_id": location_id,
            "qu_id_stock": unit_id,
            "qu_id_purchase": unit_id,
            "min_stock_amount": 0,
        }
        res = await _call("POST", "/objects/products", json=body)
        new_id = res.get("created_object_id") if isinstance(res, dict) else None
        if new_id is None:
            raise _GrocyError("missing_fk", "Grocy did not return a new product id.", "")
        return {"id": int(new_id), "name": name.strip(), "created": True}

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
        try:
            info = await _call("GET", "/system/info")
        except _GrocyError as exc:
            return exc.payload()
        version = None
        if isinstance(info, dict):
            version = (info.get("grocy_version") or {}).get("Version")
        log.info("grocy reachable; version=%s", version)
        return {"ok": True, "grocy_version": version, "notes": f"Connected to Grocy {version}."}

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

    @mcp.tool(
        name="grocy_ensure_location",
        description=(
            "Idempotently ensure a storage location exists (e.g. 'Chest Freezer'). "
            "Matches case-insensitively by exact name; creates only if absent. "
            "Returns {id, name, created}. Use before stocking an item into a "
            "location that may not exist yet."
        ),
    )
    async def ensure_location(
        name: Annotated[str, Field(min_length=1, description="Location name, e.g. 'Pantry'.")],
        description: Annotated[str, Field(description="Optional description.")] = "",
    ) -> dict[str, Any]:
        try:
            return await _ensure(
                "locations", name.strip(), {"name": name.strip(), "description": description}
            )
        except _GrocyError as exc:
            return exc.payload()

    @mcp.tool(
        name="grocy_ensure_unit",
        description=(
            "Idempotently ensure a quantity unit exists (e.g. 'count', 'lb', "
            "'pack'). Matches case-insensitively by exact name; creates only if "
            "absent. `name_plural` defaults to the singular. Returns {id, name, "
            "created}. Use before creating a product that needs a new unit."
        ),
    )
    async def ensure_unit(
        name: Annotated[str, Field(min_length=1, description="Unit name, e.g. 'count'.")],
        name_plural: Annotated[str, Field(description="Plural form; defaults to name.")] = "",
    ) -> dict[str, Any]:
        plural = name_plural.strip() or name.strip()
        try:
            return await _ensure(
                "quantity_units", name.strip(), {"name": name.strip(), "name_plural": plural}
            )
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
            enriched = [await _enrich(p) for p in matches[:limit]]
        except _GrocyError as exc:
            return exc.payload()
        return {"count": len(enriched), "products": enriched}

    @mcp.tool(
        name="grocy_list_stock",
        description=(
            "List every product currently in stock with amount on hand, stock unit, "
            "and next due date. Use for 'what do we have?' / 'how much X is left?'. "
            "For items needing attention (expiring/overdue/missing) use "
            "`grocy_expiring` instead."
        ),
    )
    async def list_stock() -> dict[str, Any]:
        try:
            data = await _call("GET", "/stock")
        except _GrocyError as exc:
            return exc.payload()
        items = data if isinstance(data, list) else []
        products = [
            {
                "id": p.get("product_id"),
                "name": (p.get("product") or {}).get("name"),
                "amount": p.get("amount"),
                "next_due_date": p.get("best_before_date"),
            }
            for p in items
        ]
        return {"count": len(products), "products": products}

    @mcp.tool(
        name="grocy_expiring",
        description=(
            "The meal-planning feed: products that are due soon, overdue, expired, "
            "or currently missing (below minimum stock). Use for 'what's going "
            "bad?', 'what should I use up?', or 'what are we out of?'. `days` sets "
            "the 'due soon' horizon."
        ),
    )
    async def expiring(
        days: Annotated[int, Field(ge=0, le=365, description="'Due soon' horizon in days.")] = 5,
    ) -> dict[str, Any]:
        try:
            data = await _call("GET", "/stock/volatile", params={"due_soon_days": days})
        except _GrocyError as exc:
            return exc.payload()
        return data if isinstance(data, dict) else {"result": data}

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
            "reconciles the product's total on hand; new stock lands at `location`.\n"
            "  • add     → append stock (a restock/purchase). Prefer this when "
            "items carry DISTINCT best-before dates you want tracked separately — "
            "each add is its own dated entry.\n"
            "  • consume → remove stock (oldest-due first). Set `spoiled=true` to "
            "record waste rather than use.\n\n"
            "NAME RESOLUTION (never guesses): exact case-insensitive match → use "
            "it. No match at all + create_if_missing → create it (result marks "
            "created=true so you can confirm verbally). A NEAR match but no exact "
            "one → returns needs_disambiguation with candidates; confirm in "
            "conversation, then re-call with `product_id`.\n\n"
            "Creating a product requires `location` and `unit` to already exist — "
            "if they don't, you get location_not_found / unit_not_found naming the "
            "missing one (run the ensure_* tool first; this tool never invents "
            "master scaffolding). For meat, always pass a real `best_before` date."
        ),
    )
    async def stock_item(
        amount: Annotated[float, Field(description="Amount > 0, in the product's stock unit.")],
        location: Annotated[str | int, Field(description="Location name or id.")],
        name: Annotated[
            str | None,
            Field(
                default=None, description="Spoken product name, e.g. 'ribeye' (or pass product_id)."
            ),
        ] = None,
        action: Annotated[str, Field(description="'set' | 'add' | 'consume'.")] = "set",
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
        product_id: Annotated[
            int | None, Field(default=None, ge=1, description="Bypass name resolution if known.")
        ] = None,
    ) -> dict[str, Any]:
        if action not in ("set", "add", "consume"):
            return _GrocyError(
                "amount_invalid", f"Unknown action '{action}'.", "Use set, add, or consume."
            ).payload()
        if amount is None or amount <= 0:
            return _GrocyError("amount_invalid", "Amount must be greater than zero.", "").payload()
        if product_id is None and not (name and name.strip()):
            return _GrocyError(
                "product_not_found", "No product name or id given.", "Pass name or product_id."
            ).payload()

        try:
            # Location is needed for the action regardless of create path.
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
                elif len(exact) > 1 or candidates:
                    cands = [await _enrich(c) for c in candidates[:10]]
                    return {
                        **_GrocyError(
                            "needs_disambiguation",
                            f"'{name}' matches existing products; not guessing.",
                            "Re-call with product_id set to the intended one.",
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
                        unit_id = await _resolve_id("quantity_units", unit, "unit_not_found")
                        product = await _create_product(name, location_id, unit_id)
                        created = True

            pid = int(product["id"])

            # ── apply the action ─────────────────────────────────────
            if action == "set":
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
                    }
                )
                await _call("POST", f"/stock/products/{pid}/add", json=body)
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
        else:
            verb = f"consumed {amount} {pname}" + (" (spoiled)" if spoiled else "")
        on_hand = "unknown" if resulting is None else resulting
        prefix = "Created and " if created else ""
        return {
            "action_taken": action,
            "product": {"id": pid, "name": pname, "created": created},
            "location": location,
            "amount": amount,
            "best_before": best_before,
            "resulting_amount_on_hand": resulting,
            "notes": f"{prefix}{verb}; {on_hand} on hand.",
        }

    # ─────────────────────────────────────────────────────────────────
    # Primitives (retained for direct use)
    # ─────────────────────────────────────────────────────────────────
    @mcp.tool(
        name="grocy_consume_product",
        description=(
            "Consume (remove) stock for a product identified by EITHER `product_id` "
            "OR `barcode` (exactly one). Set `spoiled=true` to record waste. A "
            "lower-level primitive; for conversational intake prefer "
            "`grocy_stock_item`."
        ),
    )
    async def consume_product(
        product_id: Annotated[int | None, Field(default=None, ge=1)] = None,
        barcode: Annotated[str | None, Field(default=None)] = None,
        amount: Annotated[float, Field(gt=0, description="Amount to remove.")] = 1,
        spoiled: Annotated[
            bool, Field(description="True when discarded rather than used.")
        ] = False,
    ) -> dict[str, Any]:
        if (product_id is None) == (barcode is None):
            return _GrocyError(
                "amount_invalid", "Provide exactly one of product_id or barcode.", ""
            ).payload()
        path = (
            f"/stock/products/by-barcode/{_enc(barcode)}/consume"
            if barcode
            else f"/stock/products/{product_id}/consume"
        )
        body = {"amount": amount, "transaction_type": "consume", "spoiled": spoiled}
        try:
            data = await _call("POST", path, json=body)
            resulting = await _amount_on_hand(product_id) if product_id else None
        except _GrocyError as exc:
            return exc.payload()
        return {
            "ok": True,
            "resulting_amount_on_hand": resulting,
            "stock_log": data,
            "notes": f"Consumed {amount}" + (" (spoiled)." if spoiled else "."),
        }

    @mcp.tool(
        name="grocy_open_product",
        description=(
            "Mark an amount of a product as opened (this can shift the applicable "
            "due date in Grocy). Identify it by EITHER `product_id` OR `barcode` "
            "(exactly one). The returned next due date reflects the post-open state."
        ),
    )
    async def open_product(
        product_id: Annotated[int | None, Field(default=None, ge=1)] = None,
        barcode: Annotated[str | None, Field(default=None)] = None,
        amount: Annotated[float, Field(gt=0, description="Amount to mark as opened.")] = 1,
    ) -> dict[str, Any]:
        if (product_id is None) == (barcode is None):
            return _GrocyError(
                "amount_invalid", "Provide exactly one of product_id or barcode.", ""
            ).payload()
        path = (
            f"/stock/products/by-barcode/{_enc(barcode)}/open"
            if barcode
            else f"/stock/products/{product_id}/open"
        )
        try:
            data = await _call("POST", path, json={"amount": amount})
            detail = await _stock_detail(product_id) if product_id else None
        except _GrocyError as exc:
            return exc.payload()
        next_due = detail.get("next_due_date") if detail else None
        return {
            "ok": True,
            "next_due_date": next_due,
            "stock_log": data,
            "notes": f"Opened {amount}.",
        }
