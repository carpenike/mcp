"""paperless-ngx tool tests.

Covers the custom-field bootstrap (create only what's missing), the
round-trip that returns an ASN, and the easy-to-miss bug in `paperless_link`:
PATCHing `custom_fields` replaces the whole list, so any pre-existing field
(notably `actual_account`) must be carried through.
"""

from __future__ import annotations

import json
import re
from collections.abc import Callable
from typing import Any

import pytest
from pytest_httpx import HTTPXMock

from homelab_mcp.config import Settings
from homelab_mcp.tools.paperless import register

BASE = "https://paperless.test"

pytestmark = pytest.mark.httpx_mock(assert_all_responses_were_requested=False)


class CapturingMCP:
    def __init__(self) -> None:
        self.tools: dict[str, Callable[..., Any]] = {}

    def tool(
        self, *, name: str, description: str = "", annotations: Any = None
    ) -> Callable[..., Any]:
        def deco(fn: Callable[..., Any]) -> Callable[..., Any]:
            self.tools[name] = fn
            return fn

        return deco


@pytest.fixture
def tools() -> dict[str, Callable[..., Any]]:
    mcp = CapturingMCP()
    register(
        mcp,  # type: ignore[arg-type]
        Settings(
            _env_file=None,  # type: ignore[call-arg]
            oauth_required=False,
            paperless_base_url=BASE,
            paperless_token="tok",
        ),
    )
    return mcp.tools


DOC = {
    "id": 7,
    "archive_serial_number": 1234,
    "title": "HELOC statement",
    "correspondent": 3,
    "created": "2026-07-01T00:00:00Z",
    "added": "2026-07-02T00:00:00Z",
    "tags": [1, 2],
    "document_type": 5,
    "custom_fields": [],
    "content": "Annual percentage rate 6.750%",
}


async def test_search_projects_and_reports_truncation(
    tools: dict[str, Callable[..., Any]], httpx_mock: HTTPXMock
) -> None:
    httpx_mock.add_response(
        url=re.compile(re.escape(BASE) + r"/api/documents/\?.*"),
        json={"count": 40, "results": [DOC]},
    )
    out = await tools["paperless_search"](query="heloc", limit=1)
    assert out["returned"] == 1
    assert out["total"] == 40
    # Never silently drop rows.
    assert out["truncated"] is True
    assert out["documents"][0]["asn"] == 1234
    assert out["documents"][0]["created"] == "2026-07-01"


async def test_search_resolves_tag_names_to_ids(
    tools: dict[str, Callable[..., Any]], httpx_mock: HTTPXMock
) -> None:
    httpx_mock.add_response(
        url=re.compile(re.escape(BASE) + r"/api/tags/\?.*"),
        json={"results": [{"id": 9, "name": "finance:statement"}]},
    )
    httpx_mock.add_response(
        url=re.compile(re.escape(BASE) + r"/api/documents/\?.*"),
        json={"count": 1, "results": [DOC]},
    )
    await tools["paperless_search"](tags=["finance:statement"])
    doc_req = [r for r in httpx_mock.get_requests() if "/api/documents/" in str(r.url)][0]
    assert "tags__id__all=9" in str(doc_req.url)


async def test_unknown_tag_is_a_structured_error(
    tools: dict[str, Callable[..., Any]], httpx_mock: HTTPXMock
) -> None:
    httpx_mock.add_response(
        url=re.compile(re.escape(BASE) + r"/api/tags/\?.*"), json={"results": []}
    )
    out = await tools["paperless_search"](tags=["nope"])
    assert out["error"]["code"] == "paperless_unknown_tag"


async def test_get_returns_full_content(
    tools: dict[str, Callable[..., Any]], httpx_mock: HTTPXMock
) -> None:
    httpx_mock.add_response(url=f"{BASE}/api/documents/7/", json=DOC)
    out = await tools["paperless_get"](document_id=7)
    assert "6.750%" in out["content"]
    assert out["asn"] == 1234


async def test_link_returns_asn(
    tools: dict[str, Callable[..., Any]], httpx_mock: HTTPXMock
) -> None:
    httpx_mock.add_response(
        url=re.compile(re.escape(BASE) + r"/api/custom_fields/\?.*"),
        json={"results": [{"id": 1, "name": "actual_account"}, {"id": 2, "name": "actual_txn"}]},
    )
    httpx_mock.add_response(url=f"{BASE}/api/documents/7/", method="GET", json=DOC)
    httpx_mock.add_response(
        url=f"{BASE}/api/documents/7/", method="PATCH", json={**DOC, "archive_serial_number": 1234}
    )

    out = await tools["paperless_link"](document_id=7, actual_txn_id="uuid-abc")
    assert out["linked"] is True
    assert out["asn"] == 1234
    assert "[doc:1234]" in out["next_step"]


async def test_link_never_creates_schema(
    tools: dict[str, Callable[..., Any]], httpx_mock: HTTPXMock
) -> None:
    """A missing field is a deployment error, not something to silently fix.

    Creating it at runtime would mean holding `add_customfield` forever to
    cover a one-time setup step.
    """
    httpx_mock.add_response(
        url=re.compile(re.escape(BASE) + r"/api/custom_fields/\?.*"),
        json={"results": [{"id": 1, "name": "actual_account"}]},
    )
    out = await tools["paperless_link"](document_id=7, actual_txn_id="uuid-abc")
    assert out["error"]["code"] == "paperless_missing_custom_field"
    # Crucially: no POST was attempted.
    assert [r for r in httpx_mock.get_requests() if r.method == "POST"] == []


async def test_link_preserves_other_custom_fields(
    tools: dict[str, Callable[..., Any]], httpx_mock: HTTPXMock
) -> None:
    """PATCH replaces the whole list — actual_account must survive."""
    httpx_mock.add_response(
        url=re.compile(re.escape(BASE) + r"/api/custom_fields/\?.*"),
        json={
            "results": [
                {"id": 1, "name": "actual_account"},
                {"id": 2, "name": "actual_txn"},
            ]
        },
    )
    doc = {**DOC, "custom_fields": [{"field": 1, "value": "USAA Checking"}]}
    httpx_mock.add_response(url=f"{BASE}/api/documents/7/", method="GET", json=doc)
    httpx_mock.add_response(url=f"{BASE}/api/documents/7/", method="PATCH", json=doc)

    await tools["paperless_link"](document_id=7, actual_txn_id="uuid-abc")
    patch = [r for r in httpx_mock.get_requests() if r.method == "PATCH"][0]
    fields = json.loads(patch.content)["custom_fields"]
    by_id = {f["field"]: f["value"] for f in fields}
    assert by_id[1] == "USAA Checking"  # preserved
    assert by_id[2] == "uuid-abc"  # set


async def test_link_replaces_rather_than_duplicates_an_existing_txn_field(
    tools: dict[str, Callable[..., Any]], httpx_mock: HTTPXMock
) -> None:
    httpx_mock.add_response(
        url=re.compile(re.escape(BASE) + r"/api/custom_fields/\?.*"),
        json={"results": [{"id": 1, "name": "actual_account"}, {"id": 2, "name": "actual_txn"}]},
    )
    doc = {**DOC, "custom_fields": [{"field": 2, "value": "old-uuid"}]}
    httpx_mock.add_response(url=f"{BASE}/api/documents/7/", method="GET", json=doc)
    httpx_mock.add_response(url=f"{BASE}/api/documents/7/", method="PATCH", json=doc)

    await tools["paperless_link"](document_id=7, actual_txn_id="new-uuid")
    patch = [r for r in httpx_mock.get_requests() if r.method == "PATCH"][0]
    fields = json.loads(patch.content)["custom_fields"]
    assert [f for f in fields if f["field"] == 2] == [{"field": 2, "value": "new-uuid"}]


async def test_unconfigured_returns_structured_error() -> None:
    mcp = CapturingMCP()
    register(
        mcp,  # type: ignore[arg-type]
        Settings(_env_file=None, oauth_required=False, paperless_base_url=""),  # type: ignore[call-arg]
    )
    out = await mcp.tools["paperless_search"](query="x")
    assert out["error"]["code"] == "paperless_not_configured"
