"""End-to-end smoke test: full server build + in-memory session round-trip."""

from __future__ import annotations

from mcp.shared.memory import create_connected_server_and_client_session

from app.server import build_server
from app.settings import Settings


async def test_smoke_full_round_trip(unconfigured_settings: Settings) -> None:
    """Build the real server and exercise initialize/list_tools/call_tool."""
    mcp = build_server(unconfigured_settings)
    async with create_connected_server_and_client_session(mcp) as session:
        await session.initialize()
        tools = (await session.list_tools()).tools
        assert any(t.name == "ping" for t in tools)

        ping = await session.call_tool("ping", {"message": "smoke"})
        assert ping.isError is False
        assert ping.content[0].text == "pong: smoke"  # type: ignore[union-attr]
