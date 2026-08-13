"""Regression tests for the process-level shared-client lifecycle.

Covers the refactor that lifted SDNClient/GraphClient ownership out of the
per-session lifespan into the process entrypoint (``_SharedClients`` holder +
``_run_process``).
"""

from __future__ import annotations

import asyncio

import httpx
import pytest
from mcp.shared.memory import create_connected_server_and_client_session

from app.sdn.client import SDNClient
from app.sdn.exceptions import SDNAuthError
from app.server import _run_process, _SharedClients, build_server
from app.settings import Settings
from tests.conftest import make_sdn_settings


class RecordingGraph:
    """Records probe/aclose events (shared by several tests below)."""

    def __init__(self) -> None:
        self.events: list[str] = []

    async def probe(self) -> bool:
        self.events.append("probe")
        return True

    async def aclose(self) -> None:
        self.events.append("aclose")


def make_settings() -> Settings:
    return Settings(sdn=make_sdn_settings(""))


def make_sdn_transport() -> httpx.MockTransport:
    return httpx.MockTransport(lambda r: httpx.Response(200, request=r))


async def test_consecutive_sessions_share_the_same_clients() -> None:
    """Two sequential in-memory sessions reuse one client; factory runs once."""
    settings = make_settings()
    built: list[SDNClient] = []
    graph = RecordingGraph()
    transport = make_sdn_transport()

    def sdn_factory() -> SDNClient:
        client = SDNClient(settings, transport=transport)
        built.append(client)
        return client

    holder = _SharedClients()
    mcp = build_server(
        settings,
        sdn_client_factory=sdn_factory,
        graph_client_factory=lambda: graph,
        shared_clients=holder,
    )

    async with create_connected_server_and_client_session(mcp) as session:
        await session.initialize()
        result = await session.call_tool("ping", {})
    assert result.content[0].text == "pong: ping"

    async with create_connected_server_and_client_session(mcp) as session:
        await session.initialize()
        result = await session.call_tool("ping", {})
    assert result.content[0].text == "pong: ping"

    assert len(built) == 1  # factory ran exactly once for both sessions
    assert holder.sdn is built[0]
    assert holder.graph is graph
    assert graph.events == ["probe"]  # probed once, never closed by sessions
    await holder.aclose()


async def test_ensure_started_concurrent_first_entry_logs_in_once() -> None:
    """Concurrent ensure_started calls (asyncio.gather) initialize only once."""
    settings = make_settings()
    events: list[str] = []
    transport = make_sdn_transport()

    class SlowInitSDN(SDNClient):
        async def initialize(self) -> None:
            await asyncio.sleep(0.01)  # widen the race window
            events.append("initialize")

    holder = _SharedClients()
    await asyncio.gather(
        *(
            holder.ensure_started(
                lambda: SlowInitSDN(settings, transport=transport),
                lambda: RecordingGraph(),
            )
            for _ in range(8)
        )
    )
    assert events == ["initialize"]
    assert holder.started is True
    await holder.aclose()


class _BoomMCP:
    """Runner stub that always fails once serving starts."""

    async def run_streamable_http_async(self) -> None:
        raise RuntimeError("boom")

    async def run_stdio_async(self) -> None:
        raise RuntimeError("boom")


async def test_run_process_closes_holder_when_runner_raises() -> None:
    """The holder is closed in finally even if the transport runner fails."""
    closed: list[str] = []
    settings = make_settings()
    transport = make_sdn_transport()

    class TrackingSDN(SDNClient):
        async def aclose(self) -> None:
            closed.append("sdn")
            await super().aclose()

    class TrackingGraph(RecordingGraph):
        async def aclose(self) -> None:
            closed.append("graph")
            await super().aclose()

    holder = _SharedClients()
    with pytest.raises(RuntimeError, match="boom"):
        await _run_process(
            _BoomMCP(),  # type: ignore[arg-type]
            holder,
            lambda: TrackingSDN(settings, transport=transport),
            lambda: TrackingGraph(),
            "streamable-http",
        )
    assert closed == ["graph", "sdn"]  # graph first, then sdn
    assert holder.sdn is None
    assert holder.graph is None


async def test_ensure_started_closes_built_clients_on_initialize_failure() -> None:
    """Fail-fast: an initialize() failure closes both built clients and raises."""
    settings = make_settings()
    events: list[str] = []
    transport = make_sdn_transport()

    class FailingSDN(SDNClient):
        async def initialize(self) -> None:
            raise SDNAuthError("Controller rejected the credentials.")

        async def aclose(self) -> None:
            events.append("sdn_aclose")
            await super().aclose()

    class TrackingGraph(RecordingGraph):
        async def aclose(self) -> None:
            events.append("graph_aclose")
            await super().aclose()

    graph = TrackingGraph()
    holder = _SharedClients()
    with pytest.raises(SDNAuthError):
        await holder.ensure_started(
            lambda: FailingSDN(settings, transport=transport),
            lambda: graph,
        )
    # Both already-built clients were closed (graph first), probe never ran,
    # and the holder stayed unstarted so a retry would build fresh clients.
    assert events == ["graph_aclose", "sdn_aclose"]
    assert "probe" not in graph.events  # probe never ran before the failure
    assert holder.started is False
    assert holder.sdn is None
    assert holder.graph is None
