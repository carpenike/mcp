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
    """Structural guarantee: adding a recipient param would break this."""
    import inspect

    params = set(inspect.signature(_tools()["signal_send"]).parameters)
    assert params == {"message"}


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
