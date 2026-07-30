"""Document-plane tools over paperless-ngx.

Transaction data carries amounts; documents carry *terms* — the HELOC's rate,
the mortgage's escrow breakdown, a 1098's deductible interest, a paystub's
401(k) decomposition. These tools let an advisor session pull those facts at
review time instead of hand-downloading PDFs.

`paperless_link` closes the loop between the two planes: it stamps the Actual
transaction's UUID onto the document and returns the document's ASN (archive
serial number) so the caller can write the matching `[doc:<ASN>]` marker into
the transaction's notes. Both halves of the link, one call.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Annotated, Any

from mcp.types import ToolAnnotations
from pydantic import Field

from homelab_mcp.tools._http import ToolError, enc, make_client, request_json

if TYPE_CHECKING:
    import httpx
    from mcp.server.fastmcp import FastMCP

    from homelab_mcp.config import Settings

log = logging.getLogger(__name__)
audit = logging.getLogger("homelab_mcp.audit")

_RO = ToolAnnotations(
    readOnlyHint=True, destructiveHint=False, idempotentHint=True, openWorldHint=False
)
# Sets a custom field on an existing document. Not read-only; idempotent
# (re-linking the same pair converges on the same state); not destructive.
_WRITE = ToolAnnotations(
    readOnlyHint=False, destructiveHint=False, idempotentHint=True, openWorldHint=False
)

# The custom fields this integration reads/writes. They are DECLARED in
# paperless (one-time setup), never created here — see `_resolve_field_ids`.
#
# `actual_txn` is written by paperless_link (transaction-level link).
# `actual_account` is the statement-level half of the join described in the
# finances repo's ARCHITECTURE.md, stamped per correspondent by paperless-ai.
# Nothing in this module writes it; it is resolved only so it can be surfaced
# on documents that already carry it.
FIELD_ACCOUNT = "actual_account"
FIELD_TXN = "actual_txn"

INSTRUCTIONS = """\
paperless_* tools read the household document archive (paperless-ngx).

- Use paperless_search to find documents, then paperless_get for the full OCR
  text of a specific one. Search returns metadata only; fetching every hit's
  text would flood the context.
- Documents are the source of truth for TERMS (rates, escrow, tax figures)
  that transaction data structurally cannot contain. Prefer them over
  inferring terms from amounts.
- paperless_link returns the document's ASN. After calling it, write the
  marker `[doc:<ASN>]` into the corresponding Actual transaction's notes —
  the link is only bidirectional once both halves exist.
"""


def register(mcp: FastMCP, settings: Settings) -> None:
    """Register the paperless_* tools on the given MCP server."""
    base = settings.paperless_base_url.rstrip("/")
    client: httpx.AsyncClient = make_client(
        headers=(
            {"Authorization": f"Token {settings.paperless_token}"}
            if settings.paperless_token
            else {}
        ),
        timeout=30.0,
    )
    # Resolved once per process, on first use.
    field_ids: dict[str, int] = {}

    def _require_config() -> None:
        if not base or not settings.paperless_token:
            raise ToolError(
                "paperless_not_configured",
                "paperless-ngx is not configured.",
                "Set HOMELAB_MCP_PAPERLESS_BASE_URL and HOMELAB_MCP_PAPERLESS_TOKEN.",
            )

    async def _api(method: str, path: str, **kw: Any) -> Any:
        _require_config()
        return await request_json(
            client,
            method,
            f"{base}{path}",
            service="paperless",
            unreachable_hint="Check HOMELAB_MCP_PAPERLESS_BASE_URL and that paperless is up.",
            **kw,
        )

    async def _resolve_field_ids() -> dict[str, int]:
        """Look up this integration's custom fields. Never creates them.

        Creating schema at runtime would mean granting this service
        `add_customfield` forever to cover a one-time setup step, and it turns
        a misconfigured instance into silent success instead of a visible
        error. The fields are declared once in paperless; a missing one is a
        deployment problem and is reported as such.
        """
        if field_ids:
            return field_ids
        data = await _api("GET", "/api/custom_fields/", params={"page_size": 200})
        for f in (data or {}).get("results", []):
            if f["name"] in (FIELD_ACCOUNT, FIELD_TXN):
                field_ids[f["name"]] = int(f["id"])
        return field_ids

    def _shape(doc: dict[str, Any]) -> dict[str, Any]:
        """Project a paperless document to the fields callers actually use."""
        return {
            "id": doc.get("id"),
            "asn": doc.get("archive_serial_number"),
            "title": doc.get("title"),
            "correspondent": doc.get("correspondent"),
            "created": (doc.get("created_date") or doc.get("created") or "")[:10] or None,
            "added": (doc.get("added") or "")[:10] or None,
            "tags": doc.get("tags"),
            "document_type": doc.get("document_type"),
            "custom_fields": doc.get("custom_fields"),
        }

    # ── search ───────────────────────────────────────────────────────
    @mcp.tool(
        annotations=_RO,
        name="paperless_search",
        description=(
            "Search the household document archive (paperless-ngx) by full-text "
            "query, tags, correspondent, and/or date range. Returns document "
            "metadata only — id, ASN, title, correspondent, date, tags and custom "
            "fields — not the document text. Use this to FIND candidates "
            "(statements, tax forms, paystubs, insurance policies), then call "
            "paperless_get on the one you need. Tags and correspondent accept "
            "either numeric ids or names."
        ),
    )
    async def paperless_search(
        query: Annotated[
            str | None, Field(description="Full-text search across document content.")
        ] = None,
        tags: Annotated[
            list[str] | None,
            Field(description="Tag names or ids; a document must carry ALL of them."),
        ] = None,
        correspondent: Annotated[str | None, Field(description="Correspondent name or id.")] = None,
        date_from: Annotated[
            str | None, Field(description="Earliest document date, 'YYYY-MM-DD'.")
        ] = None,
        date_to: Annotated[
            str | None, Field(description="Latest document date, 'YYYY-MM-DD'.")
        ] = None,
        limit: Annotated[int, Field(ge=1, le=100)] = 20,
    ) -> dict[str, Any]:
        try:
            params: dict[str, Any] = {"page_size": limit, "ordering": "-created"}
            if query:
                params["query"] = query
            if date_from:
                params["created__date__gte"] = date_from
            if date_to:
                params["created__date__lte"] = date_to

            if correspondent:
                if correspondent.isdigit():
                    params["correspondent__id"] = int(correspondent)
                else:
                    params["correspondent__name__iexact"] = correspondent

            if tags:
                ids: list[str] = []
                names: list[str] = []
                for t in tags:
                    (ids if str(t).isdigit() else names).append(str(t))
                for name in names:
                    found = await _api(
                        "GET", "/api/tags/", params={"name__iexact": name, "page_size": 1}
                    )
                    results = (found or {}).get("results") or []
                    if not results:
                        raise ToolError(
                            "paperless_unknown_tag",
                            f"No tag named {name!r}.",
                            "Check the tag name, or pass its numeric id.",
                        )
                    ids.append(str(results[0]["id"]))
                if ids:
                    params["tags__id__all"] = ",".join(ids)

            data = await _api("GET", "/api/documents/", params=params)
        except ToolError as err:
            return err.payload()

        results = (data or {}).get("results") or []
        total = int((data or {}).get("count") or len(results))
        return {
            "returned": len(results),
            "total": total,
            "truncated": total > len(results),
            "documents": [_shape(d) for d in results],
        }

    # ── get ──────────────────────────────────────────────────────────
    @mcp.tool(
        annotations=_RO,
        name="paperless_get",
        description=(
            "Fetch one document's full OCR text plus its metadata, by document "
            "id (from paperless_search). Use this to read the terms a statement "
            "or tax form actually states — interest rate, escrow breakdown, "
            "minimum due, deductible interest — rather than inferring them from "
            "transaction amounts. Returns the complete extracted text, which can "
            "be long."
        ),
    )
    async def paperless_get(
        document_id: Annotated[int, Field(ge=1, description="Document id.")],
    ) -> dict[str, Any]:
        try:
            doc = await _api("GET", f"/api/documents/{enc(str(document_id))}/")
        except ToolError as err:
            return err.payload()
        out = _shape(doc or {})
        out["content"] = (doc or {}).get("content")
        return out

    # ── link ─────────────────────────────────────────────────────────
    @mcp.tool(
        annotations=_WRITE,
        name="paperless_link",
        description=(
            "Link a paperless document to a specific Actual transaction by "
            "setting the document's `actual_txn` custom field to the "
            "transaction's UUID. Returns the document's ASN so you can complete "
            "the other half of the link by writing `[doc:<ASN>]` into that "
            "transaction's notes in Actual. Use sparingly and deliberately — for "
            "large one-offs, checks, warranty receipts and tax items — not for "
            "bulk matching. The `actual_txn` custom field must already exist in "
            "paperless; this tool does not create schema."
        ),
    )
    async def paperless_link(
        document_id: Annotated[int, Field(ge=1, description="Document id.")],
        actual_txn_id: Annotated[str, Field(min_length=1, description="Actual transaction UUID.")],
    ) -> dict[str, Any]:
        try:
            txn_id = (actual_txn_id or "").strip()
            if not txn_id:
                raise ToolError(
                    "paperless_bad_txn_id",
                    "actual_txn_id must be non-empty.",
                    "Pass the Actual transaction's UUID.",
                )
            fields = await _resolve_field_ids()
            if FIELD_TXN not in fields:
                raise ToolError(
                    "paperless_missing_custom_field",
                    f"paperless has no custom field named {FIELD_TXN!r}.",
                    f"Create it once in paperless (Manage → Custom Fields) as a "
                    f"'{FIELD_TXN}' string field. This service deliberately has no "
                    "permission to create schema.",
                )
            doc = await _api("GET", f"/api/documents/{enc(str(document_id))}/")

            # Preserve any other custom fields already on the document — PATCHing
            # `custom_fields` replaces the whole list, so dropping them here would
            # silently wipe e.g. actual_account.
            keep = [
                f
                for f in (doc or {}).get("custom_fields") or []
                if f.get("field") != fields[FIELD_TXN]
            ]
            keep.append({"field": fields[FIELD_TXN], "value": txn_id})

            updated = await _api(
                "PATCH",
                f"/api/documents/{enc(str(document_id))}/",
                json={"custom_fields": keep},
            )
        except ToolError as err:
            return err.payload()

        asn = (updated or {}).get("archive_serial_number")
        audit.info("paperless_link doc=%s txn=%s asn=%s", document_id, txn_id, asn)
        return {
            "linked": True,
            "document_id": document_id,
            "asn": asn,
            "actual_txn": txn_id,
            "next_step": (
                f"Write '[doc:{asn}]' into the Actual transaction's notes to complete "
                "the bidirectional link."
                if asn is not None
                else "This document has no ASN assigned in paperless; assign one to "
                "make the link human-usable from the Actual side."
            ),
        }
