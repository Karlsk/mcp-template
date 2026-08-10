"""Ping probe tool — run a ping from an SDN device and return the raw output.

Thin adapter over :class:`SDNClient.ping` (POST
``/restconf/operations/oper-rpc:ping``). The controller wraps the probe
parameters in an ``input`` object and answers with ``{"output":
{"ping-result": ...}}``; the tool flattens the result into ``ping_result``.
"""

from __future__ import annotations

import logging
from typing import Any, Literal

from mcp.server.fastmcp import Context, FastMCP

from app.sdn import SDNClient, SDNError
from app.tools.validation import skeleton_payload, unexpected_payload

logger = logging.getLogger(__name__)


def register(mcp: FastMCP) -> None:
    """Register the ping probe tool."""

    @mcp.tool(
        name="sdn_ping",
        description=(
            "Ping a destination address from an SDN device and return the raw "
            "ping output text. Required: pe_name (the device to ping from) and "
            "dest_address. Optional: source_address, vrf_name, and "
            "address_family (ipv4/ipv6, default ipv4). Returns {ok, configured, "
            "ping_result} where ping_result is the verbatim device ping output."
        ),
    )
    async def sdn_ping(
        ctx: Context[Any, Any, Any],
        pe_name: str,
        dest_address: str,
        address_family: Literal["ipv4", "ipv6"] = "ipv4",
        source_address: str | None = None,
        vrf_name: str | None = None,
    ) -> dict[str, object]:
        pe_name = pe_name.strip()
        dest_address = dest_address.strip()
        if not pe_name or not dest_address:
            return {
                "ok": False,
                "detail": "pe_name and dest_address must not be empty",
            }
        try:
            sdn: SDNClient = ctx.request_context.lifespan_context["sdn_client"]
            if not sdn.configured:
                return skeleton_payload()
            result = await sdn.ping(
                pe_name,
                dest_address,
                address_family=address_family,
                source_address=source_address.strip() if source_address else None,
                vrf_name=vrf_name.strip() if vrf_name else None,
            )
            return {
                "ok": True,
                "configured": True,
                "ping_result": result.output.ping_result or "",
            }
        except SDNError as exc:
            await ctx.error(f"sdn_ping failed: {exc}")
            return {"ok": False, "configured": True, "detail": str(exc)}
        except Exception:
            logger.exception("sdn_ping unexpected failure")
            return unexpected_payload()
