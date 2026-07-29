"""MCP tool registration aggregator (the extension seam).

Register a new tool module by adding ``its_module.register(mcp)`` below.
"""

from __future__ import annotations

from mcp.server.fastmcp import FastMCP


def register_all(mcp: FastMCP) -> None:
    """Register every tool module with the given FastMCP server."""
    from . import (
        alert_tools,
        cmd_tools,
        device_tools,
        link_tools,
        log_tools,
        perf_tools,
        sdn_tools,
        system,
        topology_tools,
    )

    system.register(mcp)
    sdn_tools.register(mcp)
    device_tools.register(mcp)
    link_tools.register(mcp)
    topology_tools.register(mcp)
    perf_tools.register(mcp)
    log_tools.register(mcp)
    alert_tools.register(mcp)
    cmd_tools.register(mcp)
