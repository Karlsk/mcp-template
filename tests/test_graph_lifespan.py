"""Tests for the dual-client lifespan wiring (SDN + SOP graph)."""

from __future__ import annotations

import httpx

from app import server
from app.graph import GraphClient
from app.sdn.client import SDNClient
from app.server import build_server, default_graph_client_factory
from app.settings import Settings
from tests.conftest import make_sdn_settings


class FakeGraphClient:
    """Records lifespan interactions: probe on startup, aclose on shutdown."""

    def __init__(self) -> None:
        self.events: list[str] = []

    async def probe(self) -> bool:
        self.events.append("probe")
        return True

    async def aclose(self) -> None:
        self.events.append("aclose")


def make_settings() -> Settings:
    return Settings(sdn=make_sdn_settings(""))


async def test_lifespan_shares_clients_across_sessions() -> None:
    """Shared semantics: consecutive sessions reuse the SAME clients.

    initialize/probe happen exactly once for the whole process, both sessions
    receive the same client objects, and exiting a session closes NOTHING
    (ownership belongs to the process-level holder).
    """
    settings = make_settings()
    events: list[str] = []
    graph = FakeGraphClient()

    class RecordingSDN(SDNClient):
        async def initialize(self) -> None:
            events.append("sdn_initialize")

        async def aclose(self) -> None:
            events.append("sdn_aclose")
            await super().aclose()

    def sdn_factory() -> RecordingSDN:
        return RecordingSDN(
            settings, transport=httpx.MockTransport(lambda r: httpx.Response(200))
        )

    holder = server._SharedClients()
    lifespan = server._make_lifespan(sdn_factory, lambda: graph, holder)
    async with lifespan(None) as context:  # type: ignore[arg-type]
        first_sdn = context["sdn_client"]
        assert context["graph_client"] is graph
        assert graph.events == ["probe"]
    async with lifespan(None) as context:  # type: ignore[arg-type]
        assert context["sdn_client"] is first_sdn
        assert context["graph_client"] is graph
    # initialize/probe ran once; session exit triggered NO aclose events.
    assert events == ["sdn_initialize"]
    assert graph.events == ["probe"]


async def test_default_graph_client_factory_builds_graph_client() -> None:
    settings = make_settings()
    client = default_graph_client_factory(settings)()
    assert isinstance(client, GraphClient)
    assert client.configured is False  # skeleton mode: no NEO4J_URI
    await client.aclose()


async def test_build_server_with_injected_graph_factory(make_session) -> None:
    """Sessions keep working with the graph factory seam in place."""
    settings = make_settings()

    def factory() -> FakeGraphClient:
        return FakeGraphClient()

    transport = httpx.MockTransport(lambda r: httpx.Response(200, request=r))
    mcp = build_server(
        settings,
        sdn_client_factory=lambda: SDNClient(settings, transport=transport),
        graph_client_factory=factory,
    )
    from mcp.shared.memory import create_connected_server_and_client_session

    async with create_connected_server_and_client_session(mcp) as session:
        await session.initialize()
        result = await session.call_tool("ping", {})
    assert result.content[0].text == "pong: ping"


async def test_make_session_still_serves_graph_skeleton(make_session) -> None:
    """Existing sessions get a skeleton GraphClient by default (no injection)."""
    settings = make_settings()
    async with make_session(settings) as session:
        await session.initialize()
        result = await session.call_tool("ping", {})
    assert result.content[0].text == "pong: ping"
