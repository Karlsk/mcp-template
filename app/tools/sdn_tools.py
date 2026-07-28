"""SDN query tools (GET operations).

Skeleton: no tools are registered yet. When wiring the real controller, add
tools here following the same pattern as ``sdn_health`` in
:mod:`app.tools.system` — **including the final ``except Exception``** that
prevents any raw exception from reaching MCP's error path::

    import logging
    from mcp.server.fastmcp import Context, FastMCP

    from app.sdn import SDNClient, SDNError

    logger = logging.getLogger(__name__)


    def register(mcp: FastMCP) -> None:
        @mcp.tool(description="List all devices known to the SDN controller.")
        async def list_devices(ctx: Context) -> dict[str, object]:
            try:
                sdn: SDNClient = ctx.request_context.lifespan_context["sdn_client"]
                devices = await sdn.get_devices()  # method to add in app/sdn/client.py
                return {"devices": [d.model_dump() for d in devices]}
            except SDNError as exc:
                await ctx.error(f"list_devices failed: {exc}")
                return {"devices": [], "error": str(exc)}
            except Exception:
                logger.exception("list_devices unexpected failure")
                return {"devices": [], "error": "Unexpected server error."}

``register_all`` in :mod:`app.tools` already calls ``register`` here, so a new
tool is live the moment it is defined — no other wiring required.
"""

from __future__ import annotations

from mcp.server.fastmcp import FastMCP


def register(mcp: FastMCP) -> None:
    """Register SDN query tools. None yet — see module docstring for the pattern."""
    # TODO(sdn-wiring): register real GET tools (devices, topology, links, ...).
    return None
