"""Tests for MCP tools over an in-memory session (no HTTP)."""

from __future__ import annotations

import json
from typing import Any

import httpx
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


async def test_sdn_alerts_success() -> None:
    """sdn_alerts returns structured result with total and data."""
    alert_data = [{"id": "1", "category": "PE端口Down", "source": "SW-1"}]

    def main_handler(_r: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200, json={"success": True, "total": 1, "message": "ok", "data": alert_data}
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
    assert payload["status_code"] == 200
    assert payload["total"] == 1
    assert payload["success"] is True
    assert payload["message"] == "ok"
    assert payload["data"] == alert_data


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
