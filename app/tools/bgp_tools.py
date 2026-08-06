"""BGP peer info tool — query one (device, peer) BGP session's details.

Thin adapter over :class:`SDNClient.get_bgp_nbr` (POST
``/controller/device-conf/bgpNbr``). Returns the local IP/interface of the
session plus the peer-device entries; unknown response fields ride extras.
"""

from __future__ import annotations

import logging
from typing import Any

from mcp.server.fastmcp import Context, FastMCP

from app.sdn import SDNClient, SDNError
from app.tools.validation import skeleton_payload, unexpected_payload

logger = logging.getLogger(__name__)


def register(mcp: FastMCP) -> None:
    """Register the BGP neighbor info tool."""

    @mcp.tool(
        name="sdn_bgp_nbr",
        description=(
            "Get BGP peer (neighbor) info for one device and peer IP: the local "
            "IP/interface of the BGP session (local_ip, local_interface) and the "
            "peer-device entries (node_name, tp_id). Both device_name and "
            "peer_ip are required. Returns {ok, configured, local_ip, "
            "local_interface, peer_device}."
        ),
    )
    async def sdn_bgp_nbr(
        ctx: Context[Any, Any, Any],
        device_name: str,
        peer_ip: str,
    ) -> dict[str, object]:
        device_name = device_name.strip()
        peer_ip = peer_ip.strip()
        if not device_name or not peer_ip:
            return {"ok": False, "detail": "device_name and peer_ip must not be empty"}
        try:
            sdn: SDNClient = ctx.request_context.lifespan_context["sdn_client"]
            if not sdn.configured:
                return skeleton_payload()
            result = await sdn.get_bgp_nbr(device_name, peer_ip)
            return {"ok": True, "configured": True, **result.model_dump()}
        except SDNError as exc:
            await ctx.error(f"sdn_bgp_nbr failed: {exc}")
            return {"ok": False, "configured": True, "detail": str(exc)}
        except Exception:
            logger.exception("sdn_bgp_nbr unexpected failure")
            return unexpected_payload()
