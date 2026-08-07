"""signal_send tests.

The safety property worth pinning: the recipient comes from configuration and
cannot be influenced by the caller. Plus the retry rule — retry transport/5xx
once, never retry a 4xx (it will fail identically and a retried send risks
double-posting).
"""

from __future__ import annotations

import re
from collections.abc import Callable
from typing import Any

import httpx
import pytest
from pytest_httpx import HTTPXMock

from homelab_mcp.config import Settings
from homelab_mcp.tools.messaging import register

BASE = "http://127.0.0.1:8484"
GROUP = "group.dGVzdGdyb3VwaWQ="
NUMBER = "+12405550100"

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


def _tools(**over: Any) -> dict[str, Callable[..., Any]]:
    cfg: dict[str, Any] = {
        "_env_file": None,
        "oauth_required": False,
        "signal_base_url": BASE,
        "signal_number": NUMBER,
        "signal_group_id": GROUP,
    }
    cfg.update(over)
    mcp = CapturingMCP()
    register(mcp, Settings(**cfg))  # type: ignore[arg-type]
    return mcp.tools


SEND_URL = re.compile(re.escape(BASE) + r"/v2/send")


async def test_send_targets_the_configured_group_only(httpx_mock: HTTPXMock) -> None:
    httpx_mock.add_response(url=SEND_URL, json={"timestamp": 1750000000000})
    out = await _tools()["signal_send"](message="pulse")
    assert out["sent"] is True
    assert out["attempts"] == 1

    sent = httpx_mock.get_requests()[0]
    import json as _json

    body = _json.loads(sent.content)
    # The recipient is config-derived; there is no tool parameter for it.
    assert body["recipients"] == [GROUP]
    assert body["number"] == NUMBER
    assert body["message"] == "pulse"


async def test_signature_exposes_no_recipient_parameter() -> None:
    """Structural guarantee: adding a free-form recipient param breaks this.

    `target` is deliberately not that. It is a closed alias enum resolved
    server-side (see test_the_schema_itself_refuses_a_raw_id); a phone number
    or group id still cannot cross this boundary.
    """
    import inspect

    params = set(inspect.signature(_tools()["signal_send"]).parameters)
    assert params == {"message", "target"}
    for forbidden in ("recipient", "recipients", "number", "group", "group_id", "to"):
        assert forbidden not in params


@pytest.mark.parametrize("bad", ["", "   ", "\n\t "])
async def test_empty_message_is_refused(bad: str) -> None:
    out = await _tools()["signal_send"](message=bad)
    assert out["error"]["code"] == "signal_empty_message"


async def test_overlong_message_is_refused() -> None:
    out = await _tools()["signal_send"](message="x" * 2001)
    assert out["error"]["code"] == "signal_message_too_long"


async def test_message_at_the_limit_is_allowed(httpx_mock: HTTPXMock) -> None:
    httpx_mock.add_response(url=SEND_URL, json={})
    out = await _tools()["signal_send"](message="x" * 2000)
    assert out["sent"] is True


async def test_transport_failure_retries_once_then_errors(httpx_mock: HTTPXMock) -> None:
    httpx_mock.add_exception(httpx.ConnectError("refused"))
    httpx_mock.add_exception(httpx.ConnectError("refused"))
    out = await _tools()["signal_send"](message="hi")
    assert out["error"]["code"] == "signal_send_failed"
    assert len(httpx_mock.get_requests()) == 2


async def test_transient_failure_then_success(httpx_mock: HTTPXMock) -> None:
    httpx_mock.add_exception(httpx.ConnectError("refused"))
    httpx_mock.add_response(url=SEND_URL, json={"timestamp": 1})
    out = await _tools()["signal_send"](message="hi")
    assert out["sent"] is True
    assert out["attempts"] == 2


async def test_4xx_is_not_retried(httpx_mock: HTTPXMock) -> None:
    """A bad request fails identically on retry; retrying risks a double-post."""
    httpx_mock.add_response(url=SEND_URL, status_code=400, text="unregistered number")
    out = await _tools()["signal_send"](message="hi")
    assert out["error"]["code"] == "signal_http_400"
    assert len(httpx_mock.get_requests()) == 1


async def test_unconfigured_returns_structured_error() -> None:
    out = await _tools(signal_base_url="")["signal_send"](message="hi")
    assert out["error"]["code"] == "signal_not_configured"


async def test_every_send_is_audit_logged(
    httpx_mock: HTTPXMock, caplog: pytest.LogCaptureFixture
) -> None:
    httpx_mock.add_response(url=SEND_URL, json={"timestamp": 42})
    with caplog.at_level("INFO", logger="homelab_mcp.audit"):
        await _tools()["signal_send"](message="pulse")
    assert any("signal_send ok" in r.getMessage() for r in caplog.records)


# ── named targets ─────────────────────────────────────────────────────

OPS_GROUP = "group.b3BzZ3JvdXBpZA=="


def _sent_recipients(httpx_mock: HTTPXMock) -> list[str]:
    import json

    reqs = [r for r in httpx_mock.get_requests() if r.method == "POST"]
    return [json.loads(r.content)["recipients"][0] for r in reqs]


async def test_default_target_is_still_the_family_group(httpx_mock: HTTPXMock) -> None:
    """Existing callers must be untouched by the addition of targets."""
    httpx_mock.add_response(url=f"{BASE}/v2/send", method="POST", json={"timestamp": 1})
    out = await _tools(signal_ops_group_id=OPS_GROUP)["signal_send"](message="hi")
    assert out["sent"] is True
    assert out["target"] == "family"
    assert _sent_recipients(httpx_mock) == [GROUP]


async def test_explicit_family_target(httpx_mock: HTTPXMock) -> None:
    httpx_mock.add_response(url=f"{BASE}/v2/send", method="POST", json={"timestamp": 1})
    out = await _tools(signal_ops_group_id=OPS_GROUP)["signal_send"](message="hi", target="family")
    assert out["target"] == "family"
    assert _sent_recipients(httpx_mock) == [GROUP]


async def test_ops_target_resolves_to_the_ops_group(httpx_mock: HTTPXMock) -> None:
    httpx_mock.add_response(url=f"{BASE}/v2/send", method="POST", json={"timestamp": 7})
    out = await _tools(signal_ops_group_id=OPS_GROUP)["signal_send"](
        message="morning check", target="ops"
    )
    assert out["sent"] is True
    assert out["target"] == "ops"
    # The ops group, and specifically NOT the family group.
    assert _sent_recipients(httpx_mock) == [OPS_GROUP]
    assert GROUP not in _sent_recipients(httpx_mock)


async def test_ops_target_fails_explicitly_when_unset(httpx_mock: HTTPXMock) -> None:
    """No fallback. A misdirected Signal message cannot be recalled.

    The dangerous failure would be quietly delivering an ops report to the
    channel both partners read.
    """
    out = await _tools()["signal_send"](message="morning check", target="ops")
    assert out["error"]["code"] == "signal_target_not_configured"
    assert "HOMELAB_MCP_SIGNAL_OPS_GROUP_ID" in out["error"]["hint"]
    # Nothing was sent anywhere.
    assert not [r for r in httpx_mock.get_requests() if r.method == "POST"]


async def test_unset_ops_does_not_fall_back_to_family(httpx_mock: HTTPXMock) -> None:
    """The specific regression worth guarding: silent delivery to the family."""
    httpx_mock.add_response(url=f"{BASE}/v2/send", method="POST", json={"timestamp": 1})
    out = await _tools()["signal_send"](message="ops only", target="ops")
    assert "error" in out
    assert GROUP not in _sent_recipients(httpx_mock)


async def test_unknown_target_is_refused_at_the_tool_boundary(
    httpx_mock: HTTPXMock,
) -> None:
    """Re-checked even though the schema is a Literal.

    A client that bypasses the JSON-schema layer must not be able to smuggle a
    raw group id in through `target`.
    """
    out = await _tools(signal_ops_group_id=OPS_GROUP)["signal_send"](
        message="hi", target="group.c29tZW9uZWVsc2U="
    )
    assert out["error"]["code"] == "signal_unknown_target"
    assert "family" in out["error"]["hint"] and "ops" in out["error"]["hint"]
    assert not [r for r in httpx_mock.get_requests() if r.method == "POST"]


async def test_target_takes_no_phone_number(httpx_mock: HTTPXMock) -> None:
    out = await _tools(signal_ops_group_id=OPS_GROUP)["signal_send"](
        message="hi", target="+12405550199"
    )
    assert out["error"]["code"] == "signal_unknown_target"
    assert not [r for r in httpx_mock.get_requests() if r.method == "POST"]


async def test_group_ids_never_appear_in_the_response(httpx_mock: HTTPXMock) -> None:
    """The id is the thing being protected; it must not leak back out."""
    httpx_mock.add_response(url=f"{BASE}/v2/send", method="POST", json={"timestamp": 3})
    out = await _tools(signal_ops_group_id=OPS_GROUP)["signal_send"](message="x", target="ops")
    blob = repr(out)
    assert OPS_GROUP not in blob
    assert GROUP not in blob


async def test_ops_works_when_only_ops_is_configured(httpx_mock: HTTPXMock) -> None:
    """Each destination stands alone; family being unset must not block ops."""
    httpx_mock.add_response(url=f"{BASE}/v2/send", method="POST", json={"timestamp": 9})
    out = await _tools(signal_group_id="", signal_ops_group_id=OPS_GROUP)["signal_send"](
        message="x", target="ops"
    )
    assert out["sent"] is True
    assert _sent_recipients(httpx_mock) == [OPS_GROUP]


async def test_family_still_fails_when_unset(httpx_mock: HTTPXMock) -> None:
    out = await _tools(signal_group_id="")["signal_send"](message="x")
    assert out["error"]["code"] == "signal_target_not_configured"
    assert not [r for r in httpx_mock.get_requests() if r.method == "POST"]


def test_the_schema_itself_refuses_a_raw_id() -> None:
    """Structural, not just a runtime check: the annotation is a closed enum.

    Resolved through get_type_hints because `from __future__ import
    annotations` leaves the raw annotation as a string.
    """
    import typing

    hints = typing.get_type_hints(_tools()["signal_send"], include_extras=False)
    args = typing.get_args(hints["target"])  # Literal[...] | None
    literals = [a for a in args if typing.get_origin(a) is typing.Literal]
    assert literals, f"target must be a Literal enum, got {hints['target']!r}"
    assert set(typing.get_args(literals[0])) == {"family", "ops"}
