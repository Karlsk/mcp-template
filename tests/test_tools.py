"""Tests for MCP tools over an in-memory session (no HTTP)."""

from __future__ import annotations

import json
from typing import Any

import httpx
import pytest
from mcp.server.fastmcp import FastMCP
from mcp.shared.memory import create_connected_server_and_client_session
from mcp.types import TextContent

from app.sdn.client import SDNClient
from app.server import build_server
from app.settings import Settings


def _payload(result: Any) -> dict[str, Any]:
    """Extract the structured dict a tool returned (structuredContent or text JSON)."""
    if getattr(result, "structuredContent", None):
        return dict(result.structuredContent)
    block = result.content[0]
    assert isinstance(block, TextContent), f"unexpected content: {block!r}"
    return json.loads(block.text)


async def test_ping_default(make_session, unconfigured_settings: Settings) -> None:
    async with make_session(unconfigured_settings) as session:
        await session.initialize()
        result = await session.call_tool("ping", {})
    block = result.content[0]
    assert isinstance(block, TextContent)
    assert block.text == "pong: ping"
    assert result.isError is False


async def test_ping_with_message(make_session, unconfigured_settings: Settings) -> None:
    async with make_session(unconfigured_settings) as session:
        await session.initialize()
        result = await session.call_tool("ping", {"message": "hello"})
    assert result.content[0].text == "pong: hello"


async def test_ping_wrong_type_is_error(make_session, unconfigured_settings: Settings) -> None:
    """Type validation must reject bad inputs and mark the result as an error."""
    async with make_session(unconfigured_settings) as session:
        await session.initialize()
        result = await session.call_tool("ping", {"message": 123})
    assert result.isError is True


async def test_list_tools_includes_system_tools(
    make_session, unconfigured_settings: Settings
) -> None:
    async with make_session(unconfigured_settings) as session:
        await session.initialize()
        tools = (await session.list_tools()).tools
    names = {t.name for t in tools}
    assert {"ping", "sdn_health"} <= names
    ping = next(t for t in tools if t.name == "ping")
    assert ping.description is not None


async def test_sdn_health_unconfigured(
    make_session, unconfigured_settings: Settings
) -> None:
    async with make_session(unconfigured_settings) as session:
        await session.initialize()
        result = await session.call_tool("sdn_health", {})
    assert result.isError is False
    payload = _payload(result)
    assert payload == {
        "ok": False,
        "configured": False,
        "detail": "SDN controller not configured (skeleton mode).",
    }


async def test_sdn_health_success(make_session, configured_settings: Settings) -> None:
    async with make_session(configured_settings, sdn_status=200) as session:
        await session.initialize()
        result = await session.call_tool("sdn_health", {})
    assert result.isError is False
    payload = _payload(result)
    assert payload["ok"] is True
    assert payload["configured"] is True
    assert payload["status_code"] == 200


async def test_sdn_health_auth_error_is_sanitized(
    make_session, configured_settings: Settings
) -> None:
    """A 401 must NOT bubble up as an MCP error; it returns a clean structured dict."""
    async with make_session(configured_settings, sdn_status=401) as session:
        await session.initialize()
        result = await session.call_tool("sdn_health", {})
    assert result.isError is False
    payload = _payload(result)
    assert payload["ok"] is False
    assert payload["configured"] is True
    # No URL or status detail must leak to the model.
    assert "https://sdn.example" not in json.dumps(payload)


async def test_sdn_health_connection_error_is_sanitized(
    make_session, configured_settings: Settings
) -> None:
    async with make_session(configured_settings, sdn_exc=httpx.ConnectError) as session:
        await session.initialize()
        result = await session.call_tool("sdn_health", {})
    assert result.isError is False
    payload = _payload(result)
    assert payload["ok"] is False


async def test_sdn_health_unexpected_error_does_not_leak(
    configured_settings: Settings,
) -> None:
    """A non-SDNError must hit the catch-all: no isError, generic message, no leak."""

    class BoomClient(SDNClient):
        async def health(self) -> Any:
            raise RuntimeError("internal boom with sensitive http://secret/url")

    mcp = build_server(
        configured_settings, sdn_client_factory=lambda: BoomClient(configured_settings)
    )
    async with create_connected_server_and_client_session(mcp) as session:
        await session.initialize()
        result = await session.call_tool("sdn_health", {})
    assert result.isError is False
    payload = _payload(result)
    assert payload == {"ok": False, "configured": False, "detail": "Unexpected server error."}
    assert "secret" not in json.dumps(payload)


# ---------------------------------------------------------------------------
# sdn_alerts tool (business POST via MCP)
# ---------------------------------------------------------------------------


def _basic_settings() -> Settings:
    from app.settings import RetrySettings, SdnSettings

    sdn = SdnSettings(
        base_url="https://sdn.example",
        auth_type="basic",
        timeout=1.0,
        retry=RetrySettings(max_retries=0, base_delay=0.0, max_delay=0.0),
        endpoints={"health": "/", "login": "/oauth/token"},
        token_field="access_token",
    )
    from pydantic import SecretStr

    return Settings(sdn=sdn, sdn_controller_username="u", sdn_controller_password=SecretStr("p"))


def _login_transport() -> httpx.MockTransport:
    def handler(request: httpx.Request) -> httpx.Response:
        assert "authorization" not in request.headers
        return httpx.Response(200, json={"access_token": "test-token"})

    return httpx.MockTransport(handler)


def _v15_server(handler: object) -> FastMCP:
    """Build a server whose SDNClient uses a custom main transport (basic mode)."""
    settings = _basic_settings()

    def factory() -> SDNClient:
        return SDNClient(
            settings,
            transport=httpx.MockTransport(handler),  # type: ignore[arg-type]
            login_transport=_login_transport(),
        )

    return build_server(settings, sdn_client_factory=factory)


def _unconfigured_server() -> FastMCP:
    from app.settings import SdnSettings

    sdn = SdnSettings(base_url="", auth_type="basic", endpoints={"health": "/"})
    settings = Settings(sdn=sdn)
    return build_server(settings, sdn_client_factory=lambda: SDNClient(settings))


async def test_sdn_alerts_success() -> None:
    """sdn_alerts delegates to the client and returns the typed result."""
    alert_data = [{"id": "1", "category": "PE端口Down", "source": "SW-1"}]

    def main_handler(_r: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={"success": True, "total": 1, "message": "ok", "code": 0, "data": alert_data},
        )

    settings = _basic_settings()
    mcp = build_server(
        settings,
        sdn_client_factory=lambda: SDNClient(
            settings,
            transport=httpx.MockTransport(main_handler),
            login_transport=_login_transport(),
        ),
    )
    async with create_connected_server_and_client_session(mcp) as session:
        await session.initialize()
        result = await session.call_tool("sdn_alerts", {})
    assert result.isError is False
    payload = _payload(result)
    assert payload["ok"] is True
    assert payload["configured"] is True
    assert payload["total"] == 1
    assert payload["success"] is True
    assert payload["message"] == "ok"
    assert payload["code"] == 0
    # data is the boundary-validated model (typed fields + defaults)
    assert payload["data"][0]["id"] == "1"
    assert payload["data"][0]["source"] == "SW-1"
    assert payload["data"][0]["category"] == "PE端口Down"


async def test_sdn_alerts_invalid_page_size() -> None:
    """Out-of-range page_size is rejected by the tool before any client call."""
    settings = _basic_settings()
    mcp = build_server(
        settings,
        sdn_client_factory=lambda: SDNClient(settings, login_transport=_login_transport()),
    )
    async with create_connected_server_and_client_session(mcp) as session:
        await session.initialize()
        result = await session.call_tool("sdn_alerts", {"page_size": 0})
    assert result.isError is False
    payload = _payload(result)
    assert payload["ok"] is False
    assert "page_size" in payload["detail"]


async def test_sdn_alerts_invalid_page_num() -> None:
    """page_num < 1 is rejected by the tool before any client call."""
    settings = _basic_settings()
    mcp = build_server(
        settings,
        sdn_client_factory=lambda: SDNClient(settings, login_transport=_login_transport()),
    )
    async with create_connected_server_and_client_session(mcp) as session:
        await session.initialize()
        result = await session.call_tool("sdn_alerts", {"page_num": 0})
    payload = _payload(result)
    assert payload["ok"] is False
    assert "page_num" in payload["detail"]


async def test_sdn_alerts_skeleton_mode() -> None:
    """When not configured, sdn_alerts returns configured=false without error."""
    from app.settings import SdnSettings

    sdn = SdnSettings(base_url="", auth_type="basic", endpoints={"health": "/"})
    settings = Settings(sdn=sdn)
    mcp = build_server(settings, sdn_client_factory=lambda: SDNClient(settings))
    async with create_connected_server_and_client_session(mcp) as session:
        await session.initialize()
        result = await session.call_tool("sdn_alerts", {})
    assert result.isError is False
    payload = _payload(result)
    assert payload == {
        "ok": False,
        "configured": False,
        "detail": "SDN controller not configured (skeleton mode).",
    }


async def test_sdn_alerts_auth_error_is_sanitized() -> None:
    """A 401 must NOT bubble up; returns clean structured dict with no URL."""
    settings = _basic_settings()
    mcp = build_server(
        settings,
        sdn_client_factory=lambda: SDNClient(
            settings,
            transport=httpx.MockTransport(lambda _r: httpx.Response(401)),
            login_transport=_login_transport(),
        ),
    )
    async with create_connected_server_and_client_session(mcp) as session:
        await session.initialize()
        result = await session.call_tool("sdn_alerts", {})
    assert result.isError is False
    payload = _payload(result)
    assert payload["ok"] is False
    assert payload["configured"] is True
    assert "https://sdn.example" not in json.dumps(payload)


async def test_sdn_alerts_connection_error_is_sanitized() -> None:
    """ConnectError -> SDNConnectionError -> clean dict, no URL leak."""
    settings = _basic_settings()

    def main_handler(_r: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("boom")

    mcp = build_server(
        settings,
        sdn_client_factory=lambda: SDNClient(
            settings,
            transport=httpx.MockTransport(main_handler),
            login_transport=_login_transport(),
        ),
    )
    async with create_connected_server_and_client_session(mcp) as session:
        await session.initialize()
        result = await session.call_tool("sdn_alerts", {})
    assert result.isError is False
    payload = _payload(result)
    assert payload["ok"] is False


async def test_sdn_alerts_unexpected_error_does_not_leak() -> None:
    """Non-SDNError hits the catch-all: no isError, generic message, no leak."""

    class BoomClient(SDNClient):
        async def request(self, method: str, endpoint: str, **kwargs: Any) -> Any:
            raise RuntimeError("internal boom with sensitive http://secret/url")

    settings = _basic_settings()
    mcp = build_server(
        settings,
        sdn_client_factory=lambda: BoomClient(settings, login_transport=_login_transport()),
    )
    async with create_connected_server_and_client_session(mcp) as session:
        await session.initialize()
        result = await session.call_tool("sdn_alerts", {})
    assert result.isError is False
    payload = _payload(result)
    assert payload == {"ok": False, "configured": False, "detail": "Unexpected server error."}
    assert "secret" not in json.dumps(payload)


# ---------------------------------------------------------------------------
# v1.5 tools (device / link / topology / perf / logs / alerts)
# ---------------------------------------------------------------------------

_EMPTY_OK = {"code": 0, "message": "ok", "data": []}


def test_page_bounds_detail_rejects_invalid() -> None:
    from app.tools.validation import page_bounds_detail

    assert page_bounds_detail(0, 10) is not None  # page_num < 1
    assert page_bounds_detail(1, 0) is not None  # page_size < 1
    assert page_bounds_detail(1, 101) is not None  # page_size > MAX
    assert page_bounds_detail(1, 10) is None  # valid


def test_time_window_detail_branches() -> None:
    from app.tools.validation import time_window_detail

    assert time_window_detail("2026-07-29 10:00:00", None) is not None  # one-sided
    assert time_window_detail("bad", "2026-07-29 10:00:00") is not None  # bad format
    assert time_window_detail("2026-07-29 11:00:00", "2026-07-29 10:00:00") is not None  # start>end
    assert time_window_detail(None, None) is None  # default window
    assert time_window_detail("2026-07-29 10:00:00", "2026-07-29 11:00:00") is None  # valid


@pytest.mark.parametrize(
    ("tool", "args"),
    [
        ("sdn_device_by_name", {"name": "X"}),
        ("sdn_device_by_management_ip", {"management_ip": "10.0.0.1"}),
        ("sdn_link_info", {}),
        ("sdn_topology", {}),
        ("sdn_port_traffic", {"device_name": "X", "port_name": "P"}),
        ("sdn_link_performance", {"link_id": "L"}),
        ("sdn_vpn_traffic", {"vpn_id": "v"}),
        ("sdn_te_tunnel_traffic", {"device_name": "X", "tunnel_name": "T"}),
        ("sdn_operation_logs", {}),
        ("sdn_device_alerts", {}),
        ("sdn_interface_name", {"device_name": "X", "interface_idx": 1}),
        ("sdn_ping", {"pe_name": "X", "dest_address": "10.0.0.1"}),
    ],
)
async def test_v15_tool_skeleton_mode(tool: str, args: dict[str, Any]) -> None:
    mcp = _unconfigured_server()
    async with create_connected_server_and_client_session(mcp) as session:
        await session.initialize()
        result = await session.call_tool(tool, args)
    assert result.isError is False
    assert _payload(result) == {
        "ok": False,
        "configured": False,
        "detail": "SDN controller not configured (skeleton mode).",
    }


async def test_list_tools_includes_v15_tools() -> None:
    mcp = _unconfigured_server()
    async with create_connected_server_and_client_session(mcp) as session:
        await session.initialize()
        names = {t.name for t in (await session.list_tools()).tools}
    assert {
        "sdn_device_by_name",
        "sdn_device_by_management_ip",
        "sdn_link_info",
        "sdn_topology",
        "sdn_port_traffic",
        "sdn_link_performance",
        "sdn_vpn_traffic",
        "sdn_te_tunnel_traffic",
        "sdn_operation_logs",
        "sdn_device_alerts",
        "sdn_interface_name",
        "sdn_ping",
    } <= names


async def test_sdn_device_by_name_success_strips_password() -> None:
    def handler(_r: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "content": [
                    {"id": "d1", "name": "n1", "management-ip": "10.0.0.1", "password": "secret"}
                ],
                "total_elements": 1,
            },
        )

    async with create_connected_server_and_client_session(_v15_server(handler)) as session:
        await session.initialize()
        result = await session.call_tool("sdn_device_by_name", {"name": "n1"})
    payload = _payload(result)
    assert payload["ok"] is True
    assert payload["content"][0]["management-ip"] == "10.0.0.1"
    assert "password" not in json.dumps(payload)


async def test_sdn_device_by_management_ip_success() -> None:
    def handler(_r: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"content": [{"id": "d1", "management-ip": "10.0.0.1"}]})

    async with create_connected_server_and_client_session(_v15_server(handler)) as session:
        await session.initialize()
        result = await session.call_tool(
            "sdn_device_by_management_ip", {"management_ip": "10.0.0.1"}
        )
    assert _payload(result)["ok"] is True


async def test_sdn_device_by_name_empty_rejected() -> None:
    server = _v15_server(lambda _r: httpx.Response(200))
    async with create_connected_server_and_client_session(server) as session:
        await session.initialize()
        result = await session.call_tool("sdn_device_by_name", {"name": "  "})
    payload = _payload(result)
    assert payload["ok"] is False
    assert "name" in payload["detail"]


async def test_sdn_link_info_success() -> None:
    def handler(_r: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"content": [{"link-id": "L1", "link-status": "UP"}]})

    async with create_connected_server_and_client_session(_v15_server(handler)) as session:
        await session.initialize()
        result = await session.call_tool("sdn_link_info", {"link_id": "L1"})
    payload = _payload(result)
    assert payload["ok"] is True
    assert payload["content"][0]["link-id"] == "L1"


async def test_sdn_topology_success() -> None:
    def handler(_r: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200, json={"topology": [{"topology-id": "t1", "node": [], "link": []}]}
        )

    server = _v15_server(handler)
    async with create_connected_server_and_client_session(server) as session:
        await session.initialize()
        result = await session.call_tool("sdn_topology", {})
    payload = _payload(result)
    assert payload["ok"] is True
    assert payload["topology"][0]["topology-id"] == "t1"


async def test_sdn_port_traffic_success() -> None:
    server = _v15_server(lambda _r: httpx.Response(200, json=_EMPTY_OK))
    async with create_connected_server_and_client_session(server) as session:
        await session.initialize()
        result = await session.call_tool(
            "sdn_port_traffic", {"device_name": "SW-1", "port_name": "GE1"}
        )
    assert _payload(result)["ok"] is True


async def test_sdn_port_traffic_bad_period_is_error() -> None:
    server = _v15_server(lambda _r: httpx.Response(200))
    async with create_connected_server_and_client_session(server) as session:
        await session.initialize()
        result = await session.call_tool(
            "sdn_port_traffic", {"device_name": "SW-1", "port_name": "GE1", "period": "2m"}
        )
    assert result.isError is True  # Literal period rejected by the MCP schema


async def test_sdn_port_traffic_one_sided_window_rejected() -> None:
    server = _v15_server(lambda _r: httpx.Response(200))
    async with create_connected_server_and_client_session(server) as session:
        await session.initialize()
        result = await session.call_tool(
            "sdn_port_traffic",
            {"device_name": "SW-1", "port_name": "GE1", "start_time": "2026-07-29 10:00:00"},
        )
    payload = _payload(result)
    assert payload["ok"] is False
    assert "together" in payload["detail"]


async def test_sdn_link_performance_success() -> None:
    server = _v15_server(lambda _r: httpx.Response(200, json=_EMPTY_OK))
    async with create_connected_server_and_client_session(server) as session:
        await session.initialize()
        result = await session.call_tool("sdn_link_performance", {"link_id": "L1"})
    assert _payload(result)["ok"] is True


async def test_sdn_vpn_traffic_success() -> None:
    server = _v15_server(lambda _r: httpx.Response(200, json=_EMPTY_OK))
    async with create_connected_server_and_client_session(server) as session:
        await session.initialize()
        result = await session.call_tool("sdn_vpn_traffic", {"vpn_id": "l2_3510"})
    assert _payload(result)["ok"] is True


async def test_sdn_te_tunnel_traffic_success() -> None:
    server = _v15_server(lambda _r: httpx.Response(200, json=_EMPTY_OK))
    async with create_connected_server_and_client_session(server) as session:
        await session.initialize()
        result = await session.call_tool(
            "sdn_te_tunnel_traffic", {"device_name": "PE-1", "tunnel_name": "Tunnel5043"}
        )
    assert _payload(result)["ok"] is True


async def test_sdn_operation_logs_success() -> None:
    def handler(_r: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"code": 0, "data": [{"id": "l1", "caller": "c"}]})

    async with create_connected_server_and_client_session(_v15_server(handler)) as session:
        await session.initialize()
        result = await session.call_tool("sdn_operation_logs", {})
    payload = _payload(result)
    assert payload["ok"] is True
    assert payload["data"][0]["caller"] == "c"


async def test_sdn_operation_logs_bad_page_rejected() -> None:
    server = _v15_server(lambda _r: httpx.Response(200))
    async with create_connected_server_and_client_session(server) as session:
        await session.initialize()
        result = await session.call_tool("sdn_operation_logs", {"page_size": 0})
    payload = _payload(result)
    assert payload["ok"] is False
    assert "page_size" in payload["detail"]


async def test_sdn_device_alerts_success() -> None:
    def handler(_r: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"code": 0, "total": 2, "data": [{"id": "a1"}]})

    async with create_connected_server_and_client_session(_v15_server(handler)) as session:
        await session.initialize()
        result = await session.call_tool("sdn_device_alerts", {"device_name": "NJ-SCT-R03"})
    payload = _payload(result)
    assert payload["ok"] is True
    assert payload["total"] == 2


async def test_sdn_device_alerts_bad_auto_recovery_rejected() -> None:
    server = _v15_server(lambda _r: httpx.Response(200))
    async with create_connected_server_and_client_session(server) as session:
        await session.initialize()
        result = await session.call_tool("sdn_device_alerts", {"auto_recovery": 9})
    payload = _payload(result)
    assert payload["ok"] is False
    assert "auto_recovery" in payload["detail"]


@pytest.mark.parametrize(
    ("tool", "args"),
    [
        ("sdn_device_by_name", {"name": "X"}),
        ("sdn_device_by_management_ip", {"management_ip": "10.0.0.1"}),
        ("sdn_link_info", {}),
        ("sdn_topology", {}),
        ("sdn_port_traffic", {"device_name": "X", "port_name": "P"}),
        ("sdn_link_performance", {"link_id": "L"}),
        ("sdn_vpn_traffic", {"vpn_id": "v"}),
        ("sdn_te_tunnel_traffic", {"device_name": "X", "tunnel_name": "T"}),
        ("sdn_operation_logs", {}),
        ("sdn_device_alerts", {}),
        ("sdn_interface_name", {"device_name": "X", "interface_idx": 1}),
        ("sdn_ping", {"pe_name": "X", "dest_address": "10.0.0.1"}),
    ],
)
async def test_v15_tool_auth_error_is_sanitized(tool: str, args: dict[str, Any]) -> None:
    server = _v15_server(lambda _r: httpx.Response(401))
    async with create_connected_server_and_client_session(server) as session:
        await session.initialize()
        result = await session.call_tool(tool, args)
    assert result.isError is False
    payload = _payload(result)
    assert payload["ok"] is False
    assert payload["configured"] is True
    assert "https://sdn.example" not in json.dumps(payload)


@pytest.mark.parametrize(
    ("tool", "args"),
    [
        ("sdn_device_by_name", {"name": "X"}),
        ("sdn_device_by_management_ip", {"management_ip": "10.0.0.1"}),
        ("sdn_link_info", {}),
        ("sdn_topology", {}),
        ("sdn_port_traffic", {"device_name": "X", "port_name": "P"}),
        ("sdn_link_performance", {"link_id": "L"}),
        ("sdn_vpn_traffic", {"vpn_id": "v"}),
        ("sdn_te_tunnel_traffic", {"device_name": "X", "tunnel_name": "T"}),
        ("sdn_operation_logs", {}),
        ("sdn_device_alerts", {}),
        ("sdn_interface_name", {"device_name": "X", "interface_idx": 1}),
        ("sdn_ping", {"pe_name": "X", "dest_address": "10.0.0.1"}),
    ],
)
async def test_v15_tool_unexpected_error_does_not_leak(
    tool: str, args: dict[str, Any]
) -> None:
    class BoomClient(SDNClient):
        async def request(self, method: str, endpoint: str, **kwargs: Any) -> Any:
            raise RuntimeError("internal boom with sensitive http://secret/url")

    settings = _basic_settings()

    def factory() -> SDNClient:
        return BoomClient(settings, login_transport=_login_transport())

    mcp = build_server(settings, sdn_client_factory=factory)
    async with create_connected_server_and_client_session(mcp) as session:
        await session.initialize()
        result = await session.call_tool(tool, args)
    assert result.isError is False
    payload = _payload(result)
    assert payload == {"ok": False, "configured": False, "detail": "Unexpected server error."}
    assert "secret" not in json.dumps(payload)


# ---------------------------------------------------------------------------
# sdn_run_command tool (POST /api/no/config/device-conf/command-result)
# ---------------------------------------------------------------------------


async def test_sdn_run_command_readonly_success() -> None:
    def handler(_r: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"result": "uptime 1 day"})

    server = _v15_server(handler)
    async with create_connected_server_and_client_session(server) as session:
        await session.initialize()
        result = await session.call_tool(
            "sdn_run_command", {"device_name": "NJ-SCT-R01", "command": "display version"}
        )
    payload = _payload(result)
    assert payload["ok"] is True
    assert payload["result"] == "uptime 1 day"


async def test_sdn_run_command_rejects_non_readonly_without_calling_client() -> None:
    called = {"n": 0}

    def handler(_r: httpx.Request) -> httpx.Response:
        called["n"] += 1
        return httpx.Response(200, json={"result": "x"})

    server = _v15_server(handler)
    async with create_connected_server_and_client_session(server) as session:
        await session.initialize()
        result = await session.call_tool(
            "sdn_run_command", {"device_name": "NJ-SCT-R01", "command": "reboot"}
        )
    payload = _payload(result)
    assert payload["ok"] is False
    assert "allow_write" in payload["detail"]
    assert called["n"] == 0  # guard rejected before any controller call


async def test_sdn_run_command_allow_write_runs_any_command() -> None:
    def handler(_r: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"result": "done"})

    server = _v15_server(handler)
    async with create_connected_server_and_client_session(server) as session:
        await session.initialize()
        result = await session.call_tool(
            "sdn_run_command",
            {"device_name": "NJ-SCT-R01", "command": "reboot", "allow_write": True},
        )
    payload = _payload(result)
    assert payload["ok"] is True
    assert payload["result"] == "done"


@pytest.mark.parametrize(
    ("args",),
    [
        ({"device_name": "", "command": "display version"},),
        ({"device_name": "NJ-SCT-R01", "command": "  "},),
    ],
)
async def test_sdn_run_command_empty_inputs_rejected(args: dict[str, Any]) -> None:
    server = _v15_server(lambda _r: httpx.Response(200, json={"result": "x"}))
    async with create_connected_server_and_client_session(server) as session:
        await session.initialize()
        result = await session.call_tool("sdn_run_command", args)
    payload = _payload(result)
    assert payload["ok"] is False
    assert "must not be empty" in payload["detail"]


async def test_sdn_run_command_skeleton_mode() -> None:
    mcp = _unconfigured_server()
    async with create_connected_server_and_client_session(mcp) as session:
        await session.initialize()
        result = await session.call_tool(
            "sdn_run_command", {"device_name": "X", "command": "display version"}
        )
    assert _payload(result) == {
        "ok": False,
        "configured": False,
        "detail": "SDN controller not configured (skeleton mode).",
    }


async def test_sdn_run_command_auth_error_is_sanitized() -> None:
    server = _v15_server(lambda _r: httpx.Response(401))
    async with create_connected_server_and_client_session(server) as session:
        await session.initialize()
        result = await session.call_tool(
            "sdn_run_command", {"device_name": "X", "command": "display version"}
        )
    payload = _payload(result)
    assert payload["ok"] is False
    assert payload["configured"] is True
    assert "https://sdn.example" not in json.dumps(payload)


# ---------------------------------------------------------------------------
# sdn_bgp_nbr tool (GET /api/no/config/device-conf/bgp-nbr)
# ---------------------------------------------------------------------------

_BGP_NBR_BODY = {
    "local_ip": "172.16.11.2",
    "local_interface": "LoopBack1",
    "peer_device": [{"node_name": "NJ-SCT-R03", "tp_id": "LoopBack1"}],
}


@pytest.mark.parametrize(
    ("args",),
    [
        ({"device_name": "", "peer_ip": "10.0.0.1"},),
        ({"device_name": "NJ-SCT-R01", "peer_ip": "  "},),
    ],
)
async def test_sdn_bgp_nbr_empty_inputs_rejected(args: dict[str, Any]) -> None:
    server = _v15_server(lambda _r: httpx.Response(200, json=_BGP_NBR_BODY))
    async with create_connected_server_and_client_session(server) as session:
        await session.initialize()
        result = await session.call_tool("sdn_bgp_nbr", args)
    payload = _payload(result)
    assert payload["ok"] is False
    assert "must not be empty" in payload["detail"]


async def test_sdn_bgp_nbr_success_envelope() -> None:
    seen: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["method"] = request.method
        seen["path"] = str(request.url.path)
        seen["params"] = dict(request.url.params)
        return httpx.Response(200, json=_BGP_NBR_BODY)

    server = _v15_server(handler)
    async with create_connected_server_and_client_session(server) as session:
        await session.initialize()
        result = await session.call_tool(
            "sdn_bgp_nbr", {"device_name": "NJ-SCT-R01", "peer_ip": "10.0.0.1"}
        )
    payload = _payload(result)
    assert payload["ok"] is True
    assert payload["configured"] is True
    assert payload["local_ip"] == "172.16.11.2"
    assert payload["local_interface"] == "LoopBack1"
    assert payload["peer_device"][0]["node_name"] == "NJ-SCT-R03"
    assert seen["method"] == "GET"
    assert seen["path"] == "/api/no/config/device-conf/bgp-nbr"
    assert seen["params"] == {"device_name": "NJ-SCT-R01", "peer_ip": "10.0.0.1"}


async def test_sdn_bgp_nbr_skeleton_mode() -> None:
    mcp = _unconfigured_server()
    async with create_connected_server_and_client_session(mcp) as session:
        await session.initialize()
        result = await session.call_tool(
            "sdn_bgp_nbr", {"device_name": "X", "peer_ip": "10.0.0.1"}
        )
    assert _payload(result) == {
        "ok": False,
        "configured": False,
        "detail": "SDN controller not configured (skeleton mode).",
    }


async def test_sdn_bgp_nbr_auth_error_is_sanitized() -> None:
    server = _v15_server(lambda _r: httpx.Response(401))
    async with create_connected_server_and_client_session(server) as session:
        await session.initialize()
        result = await session.call_tool(
            "sdn_bgp_nbr", {"device_name": "X", "peer_ip": "10.0.0.1"}
        )
    payload = _payload(result)
    assert payload["ok"] is False
    assert payload["configured"] is True
    assert "https://sdn.example" not in json.dumps(payload)


async def test_sdn_bgp_nbr_unexpected_error_does_not_leak() -> None:
    class BoomClient(SDNClient):
        async def get_bgp_nbr(self, device_name: str, peer_ip: str) -> Any:
            raise RuntimeError("internal boom with sensitive http://secret/url")

    settings = _basic_settings()
    mcp = build_server(
        settings,
        sdn_client_factory=lambda: BoomClient(settings, login_transport=_login_transport()),
    )
    async with create_connected_server_and_client_session(mcp) as session:
        await session.initialize()
        result = await session.call_tool(
            "sdn_bgp_nbr", {"device_name": "X", "peer_ip": "1.1.1.1"}
        )
    assert result.isError is False
    payload = _payload(result)
    assert payload == {"ok": False, "configured": False, "detail": "Unexpected server error."}


# ---------------------------------------------------------------------------
# sdn_isis_nbr tool (POST topology/isisNbr)
# ---------------------------------------------------------------------------

_ISIS_NBR_BODY = {
    "device_name": "NJ-SCT-R01",
    "interface_name": "Ten-GigabitEthernet3/1/10",
}


@pytest.mark.parametrize(
    ("args",),
    [
        ({"device_name": "", "interface_name": "GigabitEthernet0/4/9"},),
        ({"device_name": "NJ-SCT-R02", "interface_name": "  "},),
    ],
)
async def test_sdn_isis_nbr_empty_inputs_rejected(args: dict[str, Any]) -> None:
    server = _v15_server(lambda _r: httpx.Response(200, json=_ISIS_NBR_BODY))
    async with create_connected_server_and_client_session(server) as session:
        await session.initialize()
        result = await session.call_tool("sdn_isis_nbr", args)
    payload = _payload(result)
    assert payload["ok"] is False
    assert "must not be empty" in payload["detail"]


async def test_sdn_isis_nbr_success_envelope() -> None:
    seen: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["method"] = request.method
        seen["path"] = str(request.url.path)
        seen["body"] = json.loads(request.content)
        return httpx.Response(200, json=_ISIS_NBR_BODY)

    server = _v15_server(handler)
    async with create_connected_server_and_client_session(server) as session:
        await session.initialize()
        result = await session.call_tool(
            "sdn_isis_nbr",
            {"device_name": "NJ-SCT-R02", "interface_name": "GigabitEthernet0/4/9"},
        )
    payload = _payload(result)
    assert payload["ok"] is True
    assert payload["configured"] is True
    assert payload["device_name"] == "NJ-SCT-R01"
    assert payload["interface_name"] == "Ten-GigabitEthernet3/1/10"
    assert seen["method"] == "POST"
    assert seen["path"] == (
        "/api/sr/config/network-topology:network-topology/topology/isisNbr"
    )
    assert seen["body"] == {
        "device_name": "NJ-SCT-R02",
        "interface_name": "GigabitEthernet0/4/9",
    }


async def test_sdn_isis_nbr_skeleton_mode() -> None:
    mcp = _unconfigured_server()
    async with create_connected_server_and_client_session(mcp) as session:
        await session.initialize()
        result = await session.call_tool(
            "sdn_isis_nbr",
            {"device_name": "X", "interface_name": "GigabitEthernet0/4/9"},
        )
    assert _payload(result) == {
        "ok": False,
        "configured": False,
        "detail": "SDN controller not configured (skeleton mode).",
    }


async def test_sdn_isis_nbr_auth_error_is_sanitized() -> None:
    server = _v15_server(lambda _r: httpx.Response(401))
    async with create_connected_server_and_client_session(server) as session:
        await session.initialize()
        result = await session.call_tool(
            "sdn_isis_nbr",
            {"device_name": "X", "interface_name": "GigabitEthernet0/4/9"},
        )
    payload = _payload(result)
    assert payload["ok"] is False
    assert payload["configured"] is True
    assert "https://sdn.example" not in json.dumps(payload)
    assert "secret" not in json.dumps(payload)


# ---------------------------------------------------------------------------
# sdn_interface_name tool (GET device-conf/interface-name)
# ---------------------------------------------------------------------------

_INTERFACE_NAME_BODY = {"result": "Ten-GigabitEthernet3/2/20"}


@pytest.mark.parametrize(
    ("args",),
    [
        ({"device_name": "", "interface_idx": 644},),
        ({"device_name": "  ", "interface_idx": 1},),
    ],
)
async def test_sdn_interface_name_empty_inputs_rejected(args: dict[str, Any]) -> None:
    server = _v15_server(lambda _r: httpx.Response(200, json=_INTERFACE_NAME_BODY))
    async with create_connected_server_and_client_session(server) as session:
        await session.initialize()
        result = await session.call_tool("sdn_interface_name", args)
    payload = _payload(result)
    assert payload["ok"] is False
    assert "device_name" in payload["detail"]


async def test_sdn_interface_name_success() -> None:
    seen: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["method"] = request.method
        seen["path"] = str(request.url.path)
        seen["params"] = dict(request.url.params)
        return httpx.Response(200, json=_INTERFACE_NAME_BODY)

    server = _v15_server(handler)
    async with create_connected_server_and_client_session(server) as session:
        await session.initialize()
        result = await session.call_tool(
            "sdn_interface_name", {"device_name": "NJ-SCT-R03", "interface_idx": 644}
        )
    payload = _payload(result)
    assert payload["ok"] is True
    assert payload["configured"] is True
    assert payload["interface_name"] == "Ten-GigabitEthernet3/2/20"
    assert seen["method"] == "GET"
    assert seen["path"] == "/api/no/config/device-conf/interface-name"
    assert seen["params"] == {"device_name": "NJ-SCT-R03", "interface_idx": "644"}


async def test_sdn_interface_name_not_found() -> None:
    server = _v15_server(lambda _r: httpx.Response(200, json={}))
    async with create_connected_server_and_client_session(server) as session:
        await session.initialize()
        result = await session.call_tool(
            "sdn_interface_name", {"device_name": "NJ-SCT-R03", "interface_idx": 1}
        )
    payload = _payload(result)
    assert payload["ok"] is False
    assert payload["configured"] is True
    assert "not found" in payload["detail"]


async def test_sdn_interface_name_skeleton_mode() -> None:
    mcp = _unconfigured_server()
    async with create_connected_server_and_client_session(mcp) as session:
        await session.initialize()
        result = await session.call_tool(
            "sdn_interface_name", {"device_name": "X", "interface_idx": 1}
        )
    assert _payload(result) == {
        "ok": False,
        "configured": False,
        "detail": "SDN controller not configured (skeleton mode).",
    }


async def test_sdn_interface_name_auth_error_is_sanitized() -> None:
    server = _v15_server(lambda _r: httpx.Response(401))
    async with create_connected_server_and_client_session(server) as session:
        await session.initialize()
        result = await session.call_tool(
            "sdn_interface_name", {"device_name": "X", "interface_idx": 1}
        )
    payload = _payload(result)
    assert payload["ok"] is False
    assert payload["configured"] is True
    assert "https://sdn.example" not in json.dumps(payload)
    assert "secret" not in json.dumps(payload)


# ---------------------------------------------------------------------------
# sdn_ping tool (POST restconf/operations/oper-rpc:ping)
# ---------------------------------------------------------------------------

_PING_BODY = {"output": {"ping-result": "5 packet(s) transmitted, 5 received"}}


@pytest.mark.parametrize(
    ("args",),
    [
        ({"pe_name": "", "dest_address": "10.0.0.1"},),
        ({"pe_name": "NJ-SCT-R01", "dest_address": "  "},),
    ],
)
async def test_sdn_ping_empty_inputs_rejected(args: dict[str, Any]) -> None:
    server = _v15_server(lambda _r: httpx.Response(200, json=_PING_BODY))
    async with create_connected_server_and_client_session(server) as session:
        await session.initialize()
        result = await session.call_tool("sdn_ping", args)
    payload = _payload(result)
    assert payload["ok"] is False
    assert "must not be empty" in payload["detail"]


async def test_sdn_ping_success_envelope() -> None:
    seen: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["method"] = request.method
        seen["path"] = str(request.url.path)
        seen["body"] = json.loads(request.content)
        return httpx.Response(200, json=_PING_BODY)

    server = _v15_server(handler)
    async with create_connected_server_and_client_session(server) as session:
        await session.initialize()
        result = await session.call_tool(
            "sdn_ping",
            {
                "pe_name": "NJ-SCT-R01",
                "dest_address": "192.168.169.2",
                "source_address": "192.168.169.1",
                "vrf_name": "vpn1",
            },
        )
    payload = _payload(result)
    assert payload["ok"] is True
    assert payload["configured"] is True
    assert payload["ping_result"] == "5 packet(s) transmitted, 5 received"
    assert seen["method"] == "POST"
    assert seen["path"] == "/restconf/operations/oper-rpc:ping"
    assert seen["body"] == {
        "input": {
            "pe-name": "NJ-SCT-R01",
            "dest-address": "192.168.169.2",
            "address-family": "ipv4",
            "source-address": "192.168.169.1",
            "vrf-name": "vpn1",
        }
    }


async def test_sdn_ping_optional_params_dropped() -> None:
    seen: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["body"] = json.loads(request.content)
        return httpx.Response(200, json=_PING_BODY)

    server = _v15_server(handler)
    async with create_connected_server_and_client_session(server) as session:
        await session.initialize()
        await session.call_tool(
            "sdn_ping", {"pe_name": "NJ-SCT-R01", "dest_address": "10.0.0.1"}
        )
    inner = seen["body"]["input"]
    assert "source-address" not in inner
    assert "vrf-name" not in inner
    assert inner["address-family"] == "ipv4"


async def test_sdn_ping_skeleton_mode() -> None:
    mcp = _unconfigured_server()
    async with create_connected_server_and_client_session(mcp) as session:
        await session.initialize()
        result = await session.call_tool(
            "sdn_ping", {"pe_name": "X", "dest_address": "10.0.0.1"}
        )
    assert _payload(result) == {
        "ok": False,
        "configured": False,
        "detail": "SDN controller not configured (skeleton mode).",
    }


async def test_sdn_ping_auth_error_is_sanitized() -> None:
    server = _v15_server(lambda _r: httpx.Response(401))
    async with create_connected_server_and_client_session(server) as session:
        await session.initialize()
        result = await session.call_tool(
            "sdn_ping", {"pe_name": "X", "dest_address": "10.0.0.1"}
        )
    payload = _payload(result)
    assert payload["ok"] is False
    assert payload["configured"] is True
    assert "https://sdn.example" not in json.dumps(payload)
    assert "secret" not in json.dumps(payload)
