"""MCP tool registration aggregator (the extension seam).

Register a new tool module by adding ``its_module.register(mcp)`` below.
"""

from __future__ import annotations

from mcp.server.fastmcp import FastMCP


def register_all(mcp: FastMCP) -> None:
    """Register every tool module with the given FastMCP server."""
    from . import sdn_tools, system

    system.register(mcp)
    sdn_tools.register(mcp)
    # TODO(sdn-wiring): register additional tool modules here.
