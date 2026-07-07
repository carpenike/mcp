"""Home Assistant tool tests.

Exercises the security gates (domain allowlist, confirm gate for
high-impact domains, entity-id validation, not-configured guard) and the
closed-loop actuation contract: `confirmed` must reflect OBSERVED state
after a post-call read-back, never the HTTP 200 from the service call.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

import pytest
from pytest_httpx import HTTPXMock

from homelab_mcp.config import Settings
from homelab_mcp.tools.ha import register

BASE = "https://ha.test"

# Polling tools re-request the same state URL; let the last matching
# response be replayed instead of erroring out on the second poll.
pytestmark = pytest.mark.httpx_mock(
    assert_all_responses_were_requested=False,
    can_send_already_matched_responses=True,
)


class CapturingMCP:
    """Collects tools registered via @mcp.tool(name=...) so tests can call them."""

    def __init__(self) -> None:
        self.tools: dict[str, Callable[..., Any]] = {}

    def tool(self, *, name: str, description: str = "") -> Callable[..., Any]:
        def deco(fn: Callable[..., Any]) -> Callable[..., Any]:
            self.tools[name] = fn
            return fn

        return deco


def _state(entity_id: str, state: str, updated: str, **attrs: Any) -> dict[str, Any]:
    return {
        "entity_id": entity_id,
        "state": state,
        "attributes": {"friendly_name": entity_id.split(".", 1)[1].title(), **attrs},
        "last_changed": updated,
        "last_updated": updated,
    }


@pytest.fixture
def tools(monkeypatch: pytest.MonkeyPatch) -> dict[str, Callable[..., Any]]:
    monkeypatch.setenv("HOMELAB_MCP_OAUTH_REQUIRED", "false")
    monkeypatch.setenv("HOMELAB_MCP_HA_BASE_URL", BASE)
    monkeypatch.setenv("HOMELAB_MCP_HA_TOKEN", "test-token")
    # `lock` is allowlisted here (it isn't by default) so the confirm gate
    # can be exercised; keep the timeout tiny so unconfirmed paths are fast.
    monkeypatch.setenv("HOMELAB_MCP_HA_DOMAIN_ALLOWLIST", '["light", "switch", "lock"]')
    monkeypatch.setenv("HOMELAB_MCP_HA_CONFIRM_TIMEOUT_SECONDS", "0.3")
    mcp = CapturingMCP()
    register(mcp, Settings())  # type: ignore[arg-type]
    return mcp.tools


@pytest.fixture
def unconfigured_tools(monkeypatch: pytest.MonkeyPatch) -> dict[str, Callable[..., Any]]:
    monkeypatch.setenv("HOMELAB_MCP_OAUTH_REQUIRED", "false")
    monkeypatch.delenv("HOMELAB_MCP_HA_BASE_URL", raising=False)
    monkeypatch.delenv("HOMELAB_MCP_HA_TOKEN", raising=False)
    mcp = CapturingMCP()
    register(mcp, Settings())  # type: ignore[arg-type]
    return mcp.tools


# ── configuration guard ──────────────────────────────────────────────


async def test_unconfigured_returns_config_error(
    unconfigured_tools: dict[str, Callable[..., Any]],
) -> None:
    out = await unconfigured_tools["ha_list_entities"]()
    assert out["error"]["code"] == "ha_not_configured"


# ── list/get ─────────────────────────────────────────────────────────


async def test_list_entities_filters_and_truncates(
    tools: dict[str, Callable[..., Any]], httpx_mock: HTTPXMock
) -> None:
    httpx_mock.add_response(
        url=f"{BASE}/api/states",
        json=[
            _state("light.office_desk_lamp", "on", "2026-07-06T00:00:00Z"),
            _state("light.kitchen", "off", "2026-07-06T00:00:00Z"),
            _state("switch.heater", "off", "2026-07-06T00:00:00Z"),
        ],
    )
    out = await tools["ha_list_entities"](domain="light", limit=1)
    assert out["total"] == 2
    assert out["returned"] == 1
    assert out["truncated"] is True
    assert out["entities"][0]["entity_id"] == "light.kitchen"

    out = await tools["ha_list_entities"](search="desk")
    assert out["total"] == 1
    assert out["entities"][0]["entity_id"] == "light.office_desk_lamp"


async def test_get_state_rejects_malformed_entity_id(
    tools: dict[str, Callable[..., Any]], httpx_mock: HTTPXMock
) -> None:
    out = await tools["ha_get_state"](entity_id="../../admin")
    assert out["error"]["code"] == "ha_invalid_entity_id"
    assert not httpx_mock.get_requests()  # rejected before any HTTP call


async def test_get_state_unknown_entity_is_structured(
    tools: dict[str, Callable[..., Any]], httpx_mock: HTTPXMock
) -> None:
    httpx_mock.add_response(url=f"{BASE}/api/states/light.ghost", status_code=404)
    out = await tools["ha_get_state"](entity_id="light.ghost")
    assert out["error"]["code"] == "ha_entity_not_found"


async def test_get_state_sends_bearer_token(
    tools: dict[str, Callable[..., Any]], httpx_mock: HTTPXMock
) -> None:
    httpx_mock.add_response(
        url=f"{BASE}/api/states/light.kitchen",
        json=_state("light.kitchen", "on", "2026-07-06T00:00:00Z"),
    )
    out = await tools["ha_get_state"](entity_id="light.kitchen")
    assert out["state"] == "on"
    assert out["available"] is True
    req = httpx_mock.get_requests()[0]
    assert req.headers["Authorization"] == "Bearer test-token"


# ── ha_call_service: closed loop ─────────────────────────────────────


async def test_call_service_confirms_observed_state_change(
    tools: dict[str, Callable[..., Any]], httpx_mock: HTTPXMock
) -> None:
    url = f"{BASE}/api/states/light.kitchen"
    httpx_mock.add_response(  # before
        url=url, json=_state("light.kitchen", "off", "2026-07-06T00:00:00Z")
    )
    httpx_mock.add_response(url=f"{BASE}/api/services/light/turn_on", json=[])
    httpx_mock.add_response(  # read-back after dispatch
        url=url, json=_state("light.kitchen", "on", "2026-07-06T00:00:05Z")
    )
    out = await tools["ha_call_service"](
        domain="light", service="turn_on", entity_id="light.kitchen"
    )
    assert out["executed"] is True
    assert out["confirmed"] is True
    assert out["before"]["state"] == "off"
    assert out["after"]["state"] == "on"
    assert "note" not in out


async def test_call_service_reports_unconfirmed_when_state_never_converges(
    tools: dict[str, Callable[..., Any]], httpx_mock: HTTPXMock
) -> None:
    """A 200 on the service call must NOT become confirmed=true (intent != outcome)."""
    url = f"{BASE}/api/states/light.kitchen"
    httpx_mock.add_response(  # before AND every poll: still off
        url=url, json=_state("light.kitchen", "off", "2026-07-06T00:00:00Z")
    )
    httpx_mock.add_response(url=f"{BASE}/api/services/light/turn_on", json=[])
    out = await tools["ha_call_service"](
        domain="light", service="turn_on", entity_id="light.kitchen"
    )
    assert out["executed"] is True
    assert out["confirmed"] is False
    assert out["after"]["state"] == "off"
    assert "did not report the expected change" in out["note"]


async def test_call_service_flags_assumed_state_devices(
    tools: dict[str, Callable[..., Any]], httpx_mock: HTTPXMock
) -> None:
    url = f"{BASE}/api/states/switch.rf_outlet"
    httpx_mock.add_response(
        url=url, json=_state("switch.rf_outlet", "off", "2026-07-06T00:00:00Z", assumed_state=True)
    )
    httpx_mock.add_response(url=f"{BASE}/api/services/switch/turn_on", json=[])
    httpx_mock.add_response(
        url=url, json=_state("switch.rf_outlet", "on", "2026-07-06T00:00:01Z", assumed_state=True)
    )
    out = await tools["ha_call_service"](
        domain="switch", service="turn_on", entity_id="switch.rf_outlet"
    )
    assert out["confirmed"] is True
    assert out["assumed_state"] is True
    assert "optimistic" in out["note"]


async def test_call_service_refuses_unallowlisted_domain(
    tools: dict[str, Callable[..., Any]], httpx_mock: HTTPXMock
) -> None:
    out = await tools["ha_call_service"](
        domain="alarm_control_panel",
        service="alarm_disarm",
        entity_id="alarm_control_panel.house",
    )
    assert out["error"]["code"] == "ha_domain_not_allowed"
    assert not httpx_mock.get_requests()  # refused before any HTTP call


async def test_call_service_checks_entity_domain_not_just_service_domain(
    tools: dict[str, Callable[..., Any]], httpx_mock: HTTPXMock
) -> None:
    """An allowlisted service domain must not smuggle in a non-allowlisted entity."""
    out = await tools["ha_call_service"](
        domain="switch", service="turn_on", entity_id="alarm_control_panel.house"
    )
    assert out["error"]["code"] == "ha_domain_not_allowed"
    assert not httpx_mock.get_requests()


async def test_call_service_high_impact_domain_previews_without_confirm(
    tools: dict[str, Callable[..., Any]], httpx_mock: HTTPXMock
) -> None:
    httpx_mock.add_response(
        url=f"{BASE}/api/states/lock.front_door",
        json=_state("lock.front_door", "locked", "2026-07-06T00:00:00Z"),
    )
    out = await tools["ha_call_service"](
        domain="lock", service="unlock", entity_id="lock.front_door"
    )
    assert out["executed"] is False
    assert out["preview"]["entity"]["state"] == "locked"
    assert not [r for r in httpx_mock.get_requests() if r.method == "POST"]


async def test_call_service_high_impact_domain_executes_with_confirm(
    tools: dict[str, Callable[..., Any]], httpx_mock: HTTPXMock
) -> None:
    url = f"{BASE}/api/states/lock.front_door"
    httpx_mock.add_response(url=url, json=_state("lock.front_door", "locked", "t0"))
    httpx_mock.add_response(url=f"{BASE}/api/services/lock/unlock", json=[])
    httpx_mock.add_response(url=url, json=_state("lock.front_door", "unlocked", "t1"))
    out = await tools["ha_call_service"](
        domain="lock", service="unlock", entity_id="lock.front_door", confirm=True
    )
    assert out["executed"] is True
    assert out["confirmed"] is True
    assert out["after"]["state"] == "unlocked"


# ── automations ──────────────────────────────────────────────────────


async def test_list_automations_reports_api_editability(
    tools: dict[str, Callable[..., Any]], httpx_mock: HTTPXMock
) -> None:
    httpx_mock.add_response(
        url=f"{BASE}/api/states",
        json=[
            _state("automation.porch_light", "on", "t0", id="1712345678901"),
            _state("automation.yaml_managed", "on", "t0"),
            _state("light.kitchen", "off", "t0"),
        ],
    )
    out = await tools["ha_list_automations"]()
    assert out["total"] == 2
    by_id = {a["entity_id"]: a for a in out["automations"]}
    assert by_id["automation.porch_light"]["editable_via_api"] is True
    assert by_id["automation.yaml_managed"]["editable_via_api"] is False


async def test_upsert_automation_previews_without_confirm(
    tools: dict[str, Callable[..., Any]], httpx_mock: HTTPXMock
) -> None:
    httpx_mock.add_response(url=f"{BASE}/api/config/automation/config/new_one", status_code=404)
    out = await tools["ha_upsert_automation"](
        automation_id="new_one", config={"alias": "Test", "triggers": [], "actions": []}
    )
    assert out["executed"] is False
    assert out["preview"]["action"] == "create"
    assert out["preview"]["existing"] is None
    assert not [r for r in httpx_mock.get_requests() if r.method == "POST"]


async def test_upsert_automation_writes_and_reads_back_with_confirm(
    tools: dict[str, Callable[..., Any]], httpx_mock: HTTPXMock
) -> None:
    url = f"{BASE}/api/config/automation/config/new_one"
    cfg = {"alias": "Test", "triggers": [], "actions": []}
    httpx_mock.add_response(url=url, status_code=404)  # existing check
    httpx_mock.add_response(url=url, method="POST", json={"result": "ok"})
    httpx_mock.add_response(url=url, json={**cfg, "id": "new_one"})  # read-back
    out = await tools["ha_upsert_automation"](automation_id="new_one", config=cfg, confirm=True)
    assert out["executed"] is True
    assert out["action"] == "create"
    assert out["config"]["id"] == "new_one"
    post = next(r for r in httpx_mock.get_requests() if r.method == "POST")
    assert b'"id": "new_one"' in post.content or b'"id":"new_one"' in post.content


async def test_upsert_automation_rejects_malformed_id(
    tools: dict[str, Callable[..., Any]], httpx_mock: HTTPXMock
) -> None:
    out = await tools["ha_upsert_automation"](
        automation_id="../core", config={"alias": "x"}, confirm=True
    )
    assert out["error"]["code"] == "ha_invalid_automation_id"
    assert not httpx_mock.get_requests()


async def test_check_config_projects_verdict(
    tools: dict[str, Callable[..., Any]], httpx_mock: HTTPXMock
) -> None:
    httpx_mock.add_response(
        url=f"{BASE}/api/config/core/check_config",
        json={"result": "valid", "errors": None, "warnings": None},
    )
    out = await tools["ha_check_config"]()
    assert out["valid"] is True


# ── error contract ───────────────────────────────────────────────────


async def test_upstream_error_is_structured_not_raised(
    tools: dict[str, Callable[..., Any]], httpx_mock: HTTPXMock
) -> None:
    """A 503 upstream must return an {error:...} payload, not raise (AGENTS rule 4)."""
    httpx_mock.add_response(url=f"{BASE}/api/states", status_code=503)
    out = await tools["ha_list_entities"]()
    assert out["error"]["code"] == "ha_http_503"
