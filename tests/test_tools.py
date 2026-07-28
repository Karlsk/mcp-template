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
