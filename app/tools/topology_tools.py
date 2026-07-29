"""Topology tool (§2.7) — fetch the full topology.

Thin adapter over :class:`SDNClient.get_topology`.
"""

from __future__ import annotations

import logging
from typing import Any

from mcp.server.fastmcp import Context, FastMCP

from app.sdn import SDNClient, SDNError
from app.tools.validation import skeleton_payload, unexpected_payload

logger = logging.getLogger(__name__)


def register(mcp: FastMCP) -> None:
    """Register the topology tool."""

    @mcp.tool(
        name="sdn_topology",
        description=(
            "Fetch the full SDN topology (nodes, termination points, links). "
            "Returns {ok, configured, topology[]}."
        ),
    )
    async def sdn_topology(ctx: Context[Any, Any, Any]) -> dict[str, object]:
        try:
            sdn: SDNClient = ctx.request_context.lifespan_context["sdn_client"]
            if not sdn.configured:
                return skeleton_payload()
            result = await sdn.get_topology()
            return {"ok": True, "configured": True, **result.model_dump(by_alias=True)}
        except SDNError as exc:
            await ctx.error(f"sdn_topology failed: {exc}")
            return {"ok": False, "configured": True, "detail": str(exc)}
        except Exception:
            logger.exception("sdn_topology unexpected failure")
            return unexpected_payload()
