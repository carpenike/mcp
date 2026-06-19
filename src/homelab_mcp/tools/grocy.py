"""Grocy household-management tools.

Wraps the self-hosted Grocy instance (grocy.holthome.net) so Claude can
answer "what's in stock / due soon?", manage the shopping list, and track
chores and tasks. We WRAP Grocy's REST API; the Grocy database stays the
source of truth.

Authentication: every request carries the `GROCY-API-KEY` header. The key
is a secret loaded from `Settings.grocy_api_key` (sops-managed env var) —
it NEVER comes from user input and is NEVER logged.

This first pass covers four of Grocy's areas:

  Stock          GET  /stock                      current stock + next due
                 GET  /stock/volatile             due-soon / overdue / expired / missing
                 GET  /stock/products/{id}         product details (by id or barcode)
                 POST .../add | .../consume | .../open
  Shopping list  GET  /objects/shopping_list       list items
                 POST /stock/shoppinglist/...       add/remove product, add missing/overdue
  Chores         GET  /chores | /chores/{id}        list + details
                 POST /chores/{id}/execute          track an execution
  Tasks          GET  /tasks                        open tasks
                 POST /tasks/{id}/complete          mark done

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


class _GrocyError(Exception):
    """Raised internally when a Grocy call fails; surfaced as an error payload.

    Carries only a human-readable message — never the API key or full
    upstream response — so it is always safe to return to the client.
    """


def _enc(segment: str) -> str:
    """URL-encode a single path segment, leaving no `/` to enable traversal."""
    return quote(segment, safe="")


def _compact(body: dict[str, Any]) -> dict[str, Any]:
    """Drop keys whose value is None so we only send fields the caller set."""
    return {k: v for k, v in body.items() if v is not None}


def register(mcp: FastMCP, settings: Settings) -> None:
    """Register grocy_* tools on the given MCP server."""
    base = settings.grocy_base_url.rstrip("/")
    api_key = settings.grocy_api_key

    async def _call(
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        json: dict[str, Any] | None = None,
    ) -> Any:
        """Make one authenticated call to Grocy and return parsed JSON (or None).

        Raises `_GrocyError` (never to the transport) on any failure so each
        tool can turn it into an `{"error": ...}` payload.
        """
        if not api_key:
            raise _GrocyError(
                "Grocy API key is not configured "
                "(set HOMELAB_MCP_GROCY_API_KEY in the environment file)."
            )
        url = f"{base}{path}"
        try:
            async with httpx.AsyncClient(
                timeout=_TIMEOUT,
                headers={"GROCY-API-KEY": api_key},
            ) as client:
                resp = await client.request(method, url, params=params, json=json)
        except httpx.HTTPError as exc:
            # Log without the key or URL query (path only) to avoid leaking secrets.
            log.warning("grocy %s %s failed: %s", method, path, exc.__class__.__name__)
            raise _GrocyError(f"request to Grocy failed: {exc}") from exc

        if resp.status_code >= 400:
            detail: str | None = None
            try:
                payload = resp.json()
                if isinstance(payload, dict):
                    detail = payload.get("error_message")
            except ValueError:
                detail = resp.text[:200] or None
            raise _GrocyError(f"Grocy returned HTTP {resp.status_code}: {detail or 'no detail'}")

        if resp.status_code == 204 or not resp.content:
            return None
        try:
            return resp.json()
        except ValueError as exc:
            raise _GrocyError("Grocy returned a non-JSON response") from exc

    def _product_path(product_id: int | None, barcode: str | None, suffix: str) -> str:
        """Build a stock-product path, by barcode when supplied else by id."""
        if barcode:
            return f"/stock/products/by-barcode/{_enc(barcode)}{suffix}"
        return f"/stock/products/{product_id}{suffix}"

    def _require_one_product(product_id: int | None, barcode: str | None) -> str | None:
        """Return an error string unless exactly one of id/barcode is given."""
        if (product_id is None) == (barcode is None):
            return "Provide exactly one of `product_id` or `barcode`."
        return None

    # ── Stock: read ─────────────────────────────────────────────────
    @mcp.tool(
        name="grocy_list_stock",
        description=(
            "List every product currently in stock in Grocy, with the amount on "
            "hand and the next due (best-before) date per product. Use for "
            "'what do we have?' or 'how much X is left?' style questions. For "
            "items that need attention (due soon, overdue, expired, missing), "
            "use `grocy_list_volatile_stock` instead."
        ),
    )
    async def list_stock() -> dict[str, Any]:
        try:
            data = await _call("GET", "/stock")
        except _GrocyError as exc:
            return {"error": str(exc)}
        items = data if isinstance(data, list) else []
        return {"count": len(items), "products": items}

    @mcp.tool(
        name="grocy_list_volatile_stock",
        description=(
            "List products that need attention: due soon, overdue, expired, or "
            "currently missing (below their minimum stock amount). Use for "
            "'what's going bad?', 'what do I need to use up?', or 'what are we "
            "out of?' questions. Returns four buckets: due_products, "
            "overdue_products, expired_products, missing_products."
        ),
    )
    async def list_volatile_stock(
        due_soon_days: Annotated[
            int,
            Field(ge=0, le=365, description="Horizon (days) for 'due soon'."),
        ] = 5,
    ) -> dict[str, Any]:
        try:
            data = await _call("GET", "/stock/volatile", params={"due_soon_days": due_soon_days})
        except _GrocyError as exc:
            return {"error": str(exc)}
        return data if isinstance(data, dict) else {"result": data}

    @mcp.tool(
        name="grocy_get_product",
        description=(
            "Get full stock details for one product — amounts (on hand / opened), "
            "next due date, last/average/current price, default location, and "
            "quantity units. Identify the product by EITHER its numeric "
            "`product_id` OR its `barcode` (exactly one). Use after "
            "`grocy_list_stock` when you need the detail behind a single product."
        ),
    )
    async def get_product(
        product_id: Annotated[
            int | None,
            Field(default=None, ge=1, description="Numeric Grocy product id."),
        ] = None,
        barcode: Annotated[
            str | None,
            Field(default=None, description="A barcode mapped to the product."),
        ] = None,
    ) -> dict[str, Any]:
        err = _require_one_product(product_id, barcode)
        if err:
            return {"error": err}
        path = (
            f"/stock/products/by-barcode/{_enc(barcode)}"
            if barcode
            else f"/stock/products/{product_id}"
        )
        try:
            data = await _call("GET", path)
        except _GrocyError as exc:
            return {"error": str(exc)}
        return data if isinstance(data, dict) else {"result": data}

    # ── Stock: write ────────────────────────────────────────────────
    @mcp.tool(
        name="grocy_add_product",
        description=(
            "Add stock for a product (a purchase by default). Identify the "
            "product by EITHER `product_id` OR `barcode` (exactly one). Returns "
            "the resulting stock log entries. Use when groceries are bought / "
            "restocked."
        ),
    )
    async def add_product(
        product_id: Annotated[int | None, Field(default=None, ge=1)] = None,
        barcode: Annotated[str | None, Field(default=None)] = None,
        amount: Annotated[
            float, Field(gt=0, description="Amount to add (in the product's stock unit).")
        ] = 1,
        best_before_date: Annotated[
            str | None,
            Field(default=None, description="Due date as YYYY-MM-DD; omit for today."),
        ] = None,
        price: Annotated[
            float | None,
            Field(default=None, ge=0, description="Price per stock unit, configured currency."),
        ] = None,
        location_id: Annotated[
            int | None,
            Field(default=None, ge=1, description="Target location; omit for product default."),
        ] = None,
    ) -> dict[str, Any]:
        err = _require_one_product(product_id, barcode)
        if err:
            return {"error": err}
        body = _compact(
            {
                "amount": amount,
                "transaction_type": "purchase",
                "best_before_date": best_before_date,
                "price": price,
                "location_id": location_id,
            }
        )
        try:
            data = await _call("POST", _product_path(product_id, barcode, "/add"), json=body)
        except _GrocyError as exc:
            return {"error": str(exc)}
        return {"ok": True, "stock_log": data}

    @mcp.tool(
        name="grocy_consume_product",
        description=(
            "Consume (remove) stock for a product. Identify it by EITHER "
            "`product_id` OR `barcode` (exactly one). Set `spoiled=true` when the "
            "amount is being thrown out rather than used. Returns the resulting "
            "stock log entries. Use when something is eaten, used up, or spoiled."
        ),
    )
    async def consume_product(
        product_id: Annotated[int | None, Field(default=None, ge=1)] = None,
        barcode: Annotated[str | None, Field(default=None)] = None,
        amount: Annotated[
            float, Field(gt=0, description="Amount to remove (in the product's stock unit).")
        ] = 1,
        spoiled: Annotated[
            bool, Field(description="True when the amount was spoiled/discarded.")
        ] = False,
        location_id: Annotated[
            int | None,
            Field(default=None, ge=1, description="Only consider stock at this location."),
        ] = None,
    ) -> dict[str, Any]:
        err = _require_one_product(product_id, barcode)
        if err:
            return {"error": err}
        body = _compact(
            {
                "amount": amount,
                "transaction_type": "consume",
                "spoiled": spoiled,
                "location_id": location_id,
            }
        )
        try:
            data = await _call("POST", _product_path(product_id, barcode, "/consume"), json=body)
        except _GrocyError as exc:
            return {"error": str(exc)}
        return {"ok": True, "stock_log": data}

    @mcp.tool(
        name="grocy_open_product",
        description=(
            "Mark an amount of a product as opened. Identify it by EITHER "
            "`product_id` OR `barcode` (exactly one). Use when a package is "
            "opened (this can change the applicable due date in Grocy)."
        ),
    )
    async def open_product(
        product_id: Annotated[int | None, Field(default=None, ge=1)] = None,
        barcode: Annotated[str | None, Field(default=None)] = None,
        amount: Annotated[float, Field(gt=0, description="Amount to mark as opened.")] = 1,
    ) -> dict[str, Any]:
        err = _require_one_product(product_id, barcode)
        if err:
            return {"error": err}
        try:
            data = await _call(
                "POST",
                _product_path(product_id, barcode, "/open"),
                json={"amount": amount},
            )
        except _GrocyError as exc:
            return {"error": str(exc)}
        return {"ok": True, "stock_log": data}

    # ── Shopping list ───────────────────────────────────────────────
    @mcp.tool(
        name="grocy_list_shopping_list",
        description=(
            "List the items on a Grocy shopping list (product id, amount, note). "
            "Omit `list_id` for all lists, or pass one to filter to a single "
            "list (the default list is id 1). Use for 'what's on the shopping "
            "list?' questions."
        ),
    )
    async def list_shopping_list(
        list_id: Annotated[
            int | None,
            Field(default=None, ge=1, description="Shopping list id; omit for all."),
        ] = None,
    ) -> dict[str, Any]:
        params = {"query[]": f"shopping_list_id={list_id}"} if list_id else None
        try:
            data = await _call("GET", "/objects/shopping_list", params=params)
        except _GrocyError as exc:
            return {"error": str(exc)}
        items = data if isinstance(data, list) else []
        return {"count": len(items), "items": items}

    @mcp.tool(
        name="grocy_add_shopping_list_product",
        description=(
            "Add a product to a shopping list (increases the amount if it is "
            "already on the list). Use when the user wants to put something on "
            "the list. Omit `list_id` to use the default list (id 1)."
        ),
    )
    async def add_shopping_list_product(
        product_id: Annotated[int, Field(ge=1, description="Product id to add.")],
        amount: Annotated[
            float, Field(gt=0, description="Amount (in the product's stock unit) to add.")
        ] = 1,
        list_id: Annotated[
            int | None, Field(default=None, ge=1, description="Target list; default is 1.")
        ] = None,
        note: Annotated[str | None, Field(default=None, description="Optional item note.")] = None,
    ) -> dict[str, Any]:
        body = _compact(
            {
                "product_id": product_id,
                "product_amount": amount,
                "list_id": list_id,
                "note": note,
            }
        )
        try:
            await _call("POST", "/stock/shoppinglist/add-product", json=body)
        except _GrocyError as exc:
            return {"error": str(exc)}
        return {"ok": True}

    @mcp.tool(
        name="grocy_remove_shopping_list_product",
        description=(
            "Remove an amount of a product from a shopping list. If the remaining "
            "amount reaches zero the item is removed entirely. Omit `list_id` to "
            "use the default list (id 1)."
        ),
    )
    async def remove_shopping_list_product(
        product_id: Annotated[int, Field(ge=1, description="Product id to remove.")],
        amount: Annotated[float, Field(gt=0, description="Amount to remove.")] = 1,
        list_id: Annotated[
            int | None, Field(default=None, ge=1, description="Target list; default is 1.")
        ] = None,
    ) -> dict[str, Any]:
        body = _compact(
            {
                "product_id": product_id,
                "product_amount": amount,
                "list_id": list_id,
            }
        )
        try:
            await _call("POST", "/stock/shoppinglist/remove-product", json=body)
        except _GrocyError as exc:
            return {"error": str(exc)}
        return {"ok": True}

    @mcp.tool(
        name="grocy_add_missing_products",
        description=(
            "Add all currently missing products (those below their minimum stock "
            "amount) to a shopping list in one shot. Omit `list_id` for the "
            "default list (id 1). Use to top up the shopping list from what's "
            "running low."
        ),
    )
    async def add_missing_products(
        list_id: Annotated[
            int | None, Field(default=None, ge=1, description="Target list; default is 1.")
        ] = None,
    ) -> dict[str, Any]:
        body = _compact({"list_id": list_id})
        try:
            await _call("POST", "/stock/shoppinglist/add-missing-products", json=body or None)
        except _GrocyError as exc:
            return {"error": str(exc)}
        return {"ok": True}

    @mcp.tool(
        name="grocy_add_overdue_products",
        description=(
            "Add all overdue products to a shopping list in one shot. Omit "
            "`list_id` for the default list (id 1)."
        ),
    )
    async def add_overdue_products(
        list_id: Annotated[
            int | None, Field(default=None, ge=1, description="Target list; default is 1.")
        ] = None,
    ) -> dict[str, Any]:
        body = _compact({"list_id": list_id})
        try:
            await _call("POST", "/stock/shoppinglist/add-overdue-products", json=body or None)
        except _GrocyError as exc:
            return {"error": str(exc)}
        return {"ok": True}

    # ── Chores ──────────────────────────────────────────────────────
    @mcp.tool(
        name="grocy_list_chores",
        description=(
            "List all chores with the next estimated execution time per chore. "
            "Use for 'what chores are due?' or 'what's coming up?' questions. For "
            "the full history/details of one chore, use `grocy_get_chore`."
        ),
    )
    async def list_chores() -> dict[str, Any]:
        try:
            data = await _call("GET", "/chores")
        except _GrocyError as exc:
            return {"error": str(exc)}
        items = data if isinstance(data, list) else []
        return {"count": len(items), "chores": items}

    @mcp.tool(
        name="grocy_get_chore",
        description=(
            "Get details for one chore: when it was last tracked, how often it "
            "has been tracked, who last did it, and the next estimated execution "
            "time. Use the numeric chore id from `grocy_list_chores`."
        ),
    )
    async def get_chore(
        chore_id: Annotated[int, Field(ge=1, description="Numeric chore id.")],
    ) -> dict[str, Any]:
        try:
            data = await _call("GET", f"/chores/{chore_id}")
        except _GrocyError as exc:
            return {"error": str(exc)}
        return data if isinstance(data, dict) else {"result": data}

    @mcp.tool(
        name="grocy_track_chore",
        description=(
            "Track an execution of a chore (marks it done now, or skipped). Omit "
            "`tracked_time` to use the current time. Set `skipped=true` to record "
            "the occurrence as skipped rather than performed. Use when a chore "
            "has been completed."
        ),
    )
    async def track_chore(
        chore_id: Annotated[int, Field(ge=1, description="Numeric chore id.")],
        tracked_time: Annotated[
            str | None,
            Field(default=None, description="ISO 8601 datetime; omit for now."),
        ] = None,
        done_by: Annotated[
            int | None,
            Field(default=None, ge=1, description="User id who did it; omit for current user."),
        ] = None,
        skipped: Annotated[
            bool, Field(description="True to record the execution as skipped.")
        ] = False,
    ) -> dict[str, Any]:
        body = _compact(
            {
                "tracked_time": tracked_time,
                "done_by": done_by,
                "skipped": skipped,
            }
        )
        try:
            data = await _call("POST", f"/chores/{chore_id}/execute", json=body)
        except _GrocyError as exc:
            return {"error": str(exc)}
        return {"ok": True, "log_entry": data}

    # ── Tasks ───────────────────────────────────────────────────────
    @mcp.tool(
        name="grocy_list_tasks",
        description=(
            "List all tasks that are not done yet, with due dates and "
            "assignee/category info. Use for 'what tasks are open / due?' "
            "questions."
        ),
    )
    async def list_tasks() -> dict[str, Any]:
        try:
            data = await _call("GET", "/tasks")
        except _GrocyError as exc:
            return {"error": str(exc)}
        items = data if isinstance(data, list) else []
        return {"count": len(items), "tasks": items}

    @mcp.tool(
        name="grocy_complete_task",
        description=(
            "Mark a task as completed. Omit `done_time` to use the current time. "
            "Use the numeric task id from `grocy_list_tasks`."
        ),
    )
    async def complete_task(
        task_id: Annotated[int, Field(ge=1, description="Numeric task id.")],
        done_time: Annotated[
            str | None,
            Field(default=None, description="ISO 8601 datetime; omit for now."),
        ] = None,
    ) -> dict[str, Any]:
        body = _compact({"done_time": done_time})
        try:
            await _call("POST", f"/tasks/{task_id}/complete", json=body or None)
        except _GrocyError as exc:
            return {"error": str(exc)}
        return {"ok": True}
