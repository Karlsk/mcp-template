"""ISIS neighbor tool — resolve the peer side of one local ISIS adjacency.

Thin adapter over :class:`SDNClient.get_isis_nbr` (POST
``topology/isisNbr``). Returns the peer device's name and its interface
facing us; unknown response fields ride extras.
"""

from __future__ import annotations

import logging
from typing import Any

from mcp.server.fastmcp import Context, FastMCP

from app.sdn import SDNClient, SDNError
from app.tools.validation import skeleton_payload, unexpected_payload

logger = logging.getLogger(__name__)


def register(mcp: FastMCP) -> None:
    """Register the ISIS neighbor info tool."""

    @mcp.tool(
        name="sdn_isis_nbr",
        description=(
            "Get the ISIS peer (neighbor) on one local interface of a device: "
            "the peer device's name and the interface facing us. Both "
            "device_name and interface_name are required. Returns {ok, "
            "configured, device_name, interface_name} where device_name/"
            "interface_name describe the peer side of the adjacency."
        ),
    )
    async def sdn_isis_nbr(
        ctx: Context[Any, Any, Any],
        device_name: str,
        interface_name: str,
    ) -> dict[str, object]:
        device_name = device_name.strip()
        interface_name = interface_name.strip()
        if not device_name or not interface_name:
            return {
                "ok": False,
                "detail": "device_name and interface_name must not be empty",
            }
        try:
            sdn: SDNClient = ctx.request_context.lifespan_context["sdn_client"]
            if not sdn.configured:
                return skeleton_payload()
            result = await sdn.get_isis_nbr(device_name, interface_name)
            return {"ok": True, "configured": True, **result.model_dump()}
        except SDNError as exc:
            await ctx.error(f"sdn_isis_nbr failed: {exc}")
            return {"ok": False, "configured": True, "detail": str(exc)}
        except Exception:
            logger.exception("sdn_isis_nbr unexpected failure")
            return unexpected_payload()
