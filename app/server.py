"""MCP server factory, per-session lifespan, and CLI entrypoint.

Streamable HTTP is the primary transport; stdio is also supported for local
clients (e.g. Claude Desktop). Select with ``--transport``.
"""

from __future__ import annotations

import argparse
from collections.abc import AsyncIterator, Callable
from contextlib import AbstractAsyncContextManager, asynccontextmanager

from mcp.server.fastmcp import FastMCP
from mcp.server.transport_security import TransportSecuritySettings

from app.common.logging import setup_logging
from app.graph.client import GraphClient
from app.sdn.client import SDNClient
from app.settings import Settings
from app.tools import register_all

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


def _make_lifespan(
    factory: SDNClientFactory,
    graph_factory: GraphClientFactory,
) -> Callable[[FastMCP], AbstractAsyncContextManager[dict[str, object]]]:
    """Build a per-session lifespan owning one SDNClient and one GraphClient."""

    @asynccontextmanager
    async def lifespan(_server: FastMCP) -> AsyncIterator[dict[str, object]]:
        client = factory()
        graph = graph_factory()
        try:
            # basic mode: fetch the first token (fail-fast at startup if the
            # controller is unreachable or rejects credentials). no-auth/bearer: no-op.
            await client.initialize()
            # Unlike the SDN basic-auth login, graph connectivity is NOT
            # fail-fast: SDN tools must keep working when the SOP graph is down.
            await graph.probe()
            yield {"sdn_client": client, "graph_client": graph}
        finally:
            await graph.aclose()
            await client.aclose()

    return lifespan


def build_server(
    settings: Settings,
    *,
    sdn_client_factory: SDNClientFactory | None = None,
    graph_client_factory: GraphClientFactory | None = None,
) -> FastMCP:
    """Construct the FastMCP server with tools registered and lifespan wired.

    ``sdn_client_factory`` / ``graph_client_factory`` let tests inject clients
    backed by an ``httpx.MockTransport`` / a fake Neo4j driver; in production
    real clients are built per session.
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
        lifespan=_make_lifespan(factory, graph_factory),
        transport_security=TransportSecuritySettings(
            enable_dns_rebinding_protection=settings.mcp_dns_rebinding_protection
        ),
    )
    register_all(mcp)
    return mcp


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

    mcp = build_server(settings)
    # Configure the app.* JSON logger once at startup from MCP_LOG_LEVEL. Done
    # after build_server so this is the final logging mutation; not in
    # build_server itself (which tests use) to avoid disturbing test logging.
    setup_logging(settings.mcp_log_level)
    mcp.run(transport=args.transport)
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
