"""MCP server factory, process-level shared clients, and CLI entrypoint.

Streamable HTTP is the primary transport; stdio is also supported for local
clients (e.g. Claude Desktop). Select with ``--transport``.

Client lifetime model: one SDNClient and one GraphClient are shared by the
WHOLE process (``_SharedClients`` holder). FastMCP runs the lifespan per
session, so the lifespan only hands the shared clients to each session —
construction, ``initialize()`` (basic mode: controller login) and ``probe()``
happen exactly once at process startup, and shutdown closes both clients
exactly once in ``_run_process``. A failing login therefore aborts process
startup (non-zero exit) instead of crashing an individual session.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
from collections.abc import AsyncIterator, Callable
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from dataclasses import dataclass, field

import anyio
from mcp.server.fastmcp import FastMCP
from mcp.server.transport_security import TransportSecuritySettings

from app.common.logging import setup_logging
from app.graph.client import GraphClient
from app.sdn.client import SDNClient
from app.sdn.exceptions import SDNError
from app.settings import Settings
from app.templates import get_registry
from app.tools import register_all

logger = logging.getLogger(__name__)

INSTRUCTIONS = (
    "SDN controller operations via MCP. "
    "Use 'ping' to verify the server and 'sdn_health' to check controller connectivity. "
    "All SDN tools return a structured status dict and never leak internal error details."
)

SDNClientFactory = Callable[[], SDNClient]
GraphClientFactory = Callable[[], GraphClient]


def default_sdn_client_factory(settings: Settings) -> SDNClientFactory:
    """Build the default factory that constructs a real SDNClient from settings."""

    def _factory() -> SDNClient:
        return SDNClient(settings)

    return _factory


def default_graph_client_factory(settings: Settings) -> GraphClientFactory:
    """Build the default factory that constructs a real GraphClient from settings."""

    def _factory() -> GraphClient:
        return GraphClient(settings)

    return _factory


@dataclass
class _SharedClients:
    """Process-level holder owning the shared SDN and graph clients.

    Ownership lives HERE (not in the per-session lifespan): ``ensure_started``
    builds and initializes both clients exactly once, and ``aclose`` is the
    single shutdown path called by the process entrypoint (``_run_process``).
    """

    sdn: SDNClient | None = None
    graph: GraphClient | None = None
    started: bool = False
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)

    async def ensure_started(
        self, factory: SDNClientFactory, graph_factory: GraphClientFactory
    ) -> None:
        """Build and initialize both clients exactly once (idempotent start).

        ``initialize()`` is fail-fast (basic mode: controller login) and NOT
        idempotent, hence the ``started`` guard; ``probe()`` never raises. On
        any startup failure both already-built clients are closed before the
        exception propagates (graph first, then sdn).
        """
        async with self.lock:
            if self.started:
                return
            sdn = factory()
            graph = graph_factory()
            try:
                await sdn.initialize()
                # Unlike the SDN basic-auth login, graph connectivity is NOT
                # fail-fast: SDN tools must keep working when the graph is down.
                await graph.probe()
            except BaseException:
                # Cleanup both clients even if the first close raises, and
                # cover cancellation (BaseException) before re-raising.
                try:
                    await graph.aclose()
                except Exception:
                    logger.warning("shared graph client cleanup failed", exc_info=True)
                try:
                    await sdn.aclose()
                except Exception:
                    logger.warning("shared sdn client cleanup failed", exc_info=True)
                raise
            self.sdn = sdn
            self.graph = graph
            self.started = True

    async def aclose(self) -> None:
        """Close both clients (graph first, then sdn). Idempotent and None-safe.

        Resets ``started`` so a later ``ensure_started`` rebuilds fresh clients
        instead of a session lifespan yielding ``None`` references.
        """
        async with self.lock:
            if self.graph is not None:
                await self.graph.aclose()
                self.graph = None
            if self.sdn is not None:
                await self.sdn.aclose()
                self.sdn = None
            self.started = False


def _make_lifespan(
    factory: SDNClientFactory,
    graph_factory: GraphClientFactory,
    holder: _SharedClients | None = None,
) -> Callable[[FastMCP], AbstractAsyncContextManager[dict[str, object]]]:
    """Build a per-session lifespan that hands out the process-shared clients.

    The clients are OWNED by ``holder`` (process lifetime), not by the session:
    the lifespan only ensures they are started and yields read-only references
    under the stable keys ``"sdn_client"`` / ``"graph_client"``. It never
    constructs beyond the first session, never initializes twice, and never
    closes — shutdown belongs to the process entrypoint (``_run_process``).
    When ``holder`` is omitted a private one is created (tests / standalone).
    """
    shared = holder if holder is not None else _SharedClients()

    @asynccontextmanager
    async def lifespan(_server: FastMCP) -> AsyncIterator[dict[str, object]]:
        await shared.ensure_started(factory, graph_factory)
        yield {"sdn_client": shared.sdn, "graph_client": shared.graph}

    return lifespan


def build_server(
    settings: Settings,
    *,
    sdn_client_factory: SDNClientFactory | None = None,
    graph_client_factory: GraphClientFactory | None = None,
    shared_clients: _SharedClients | None = None,
) -> FastMCP:
    """Construct the FastMCP server with tools registered and lifespan wired.

    ``sdn_client_factory`` / ``graph_client_factory`` let tests inject clients
    backed by an ``httpx.MockTransport`` / a fake Neo4j driver. The built
    clients are shared process-wide via ``shared_clients`` (a private holder is
    created when omitted, e.g. by tests that only exercise sessions).
    ``shared_clients`` is an internal/test seam — the private ``_SharedClients``
    type appearing in this signature is deliberately NOT a stable public API.
    """
    factory = sdn_client_factory or default_sdn_client_factory(settings)
    graph_factory = graph_client_factory or default_graph_client_factory(settings)
    mcp = FastMCP(
        "sdn-mcp",
        instructions=INSTRUCTIONS,
        host=settings.mcp_host,
        port=settings.mcp_port,
        streamable_http_path="/mcp",
        log_level=settings.mcp_log_level,
        lifespan=_make_lifespan(factory, graph_factory, shared_clients),
        transport_security=TransportSecuritySettings(
            enable_dns_rebinding_protection=settings.mcp_dns_rebinding_protection
        ),
    )
    register_all(mcp)
    return mcp


async def _run_process(
    mcp: FastMCP,
    holder: _SharedClients,
    factory: SDNClientFactory,
    graph_factory: GraphClientFactory,
    transport: str,
) -> None:
    """Serve until shutdown, owning the shared clients for the process lifetime.

    Startup login/probe happens BEFORE the transport accepts connections; a
    failure raises (-> non-zero process exit). The synchronous ``mcp.run()``
    must NOT be called here — it wraps its own ``anyio.run`` and cannot be
    nested inside one.
    """
    try:
        await holder.ensure_started(factory, graph_factory)
        if transport == "stdio":
            await mcp.run_stdio_async()
        else:
            await mcp.run_streamable_http_async()
    finally:
        await holder.aclose()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="sdn-mcp")
    parser.add_argument("--host", default=None, help="Bind host (Streamable HTTP).")
    parser.add_argument("--port", type=int, default=None, help="Bind port (Streamable HTTP).")
    parser.add_argument(
        "--transport",
        choices=["streamable-http", "stdio"],
        default="streamable-http",
        help="MCP transport (default: streamable-http).",
    )
    args = parser.parse_args(argv)

    settings = Settings()
    overrides: dict[str, object] = {}
    if args.host is not None:
        overrides["mcp_host"] = args.host
    if args.port is not None:
        overrides["mcp_port"] = args.port
    if overrides:
        settings = settings.model_copy(update=overrides)

    factory = default_sdn_client_factory(settings)
    graph_factory = default_graph_client_factory(settings)
    holder = _SharedClients()
    mcp = build_server(
        settings,
        sdn_client_factory=factory,
        graph_client_factory=graph_factory,
        shared_clients=holder,
    )
    # Configure the app.* JSON logger once at startup from MCP_LOG_LEVEL. Done
    # after build_server so this is the final logging mutation; not in
    # build_server itself (which tests use) to avoid disturbing test logging.
    setup_logging(settings.mcp_log_level)
    # Fail fast on a malformed template library instead of surfacing it as a tool
    # error on the first agent call.
    get_registry()
    try:
        anyio.run(_run_process, mcp, holder, factory, graph_factory, args.transport)
    except SDNError as exc:
        # Fail-fast startup failure (e.g. basic-mode login rejected): report
        # the safe public message and exit non-zero — no stack-trace noise.
        logger.error("startup failed: %s", exc)
        return 1
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
