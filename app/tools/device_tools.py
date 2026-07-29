"""Device query tools (§2.2) — look up devices by name or management IP.

Thin adapters over :class:`SDNClient.query_devices`. Each tool resolves the
client from the lifespan context, validates its inputs, calls ONE client method,
and returns a structured dict. Endpoint/body/parsing live in the client.
"""

from __future__ import annotations

import logging
from typing import Any

from mcp.server.fastmcp import Context, FastMCP

from app.sdn import SDNClient, SDNError
from app.tools.validation import page_bounds_detail, skeleton_payload, unexpected_payload

logger = logging.getLogger(__name__)


def register(mcp: FastMCP) -> None:
    """Register device query tools."""

    @mcp.tool(
        name="sdn_device_by_name",
        description=(
            "Look up SDN devices by name (paged; page_num is 1-based). "
            "Returns {ok, configured, content[], total_elements, number, ...}. "
            "Device output never includes credentials (password/community)."
        ),
    )
    async def sdn_device_by_name(
        ctx: Context[Any, Any, Any],
        name: str,
        page_num: int = 1,
        page_size: int = 10,
    ) -> dict[str, object]:
        name = name.strip()
        if not name:
            return {"ok": False, "detail": "name must not be empty"}
        if detail := page_bounds_detail(page_num, page_size):
            return {"ok": False, "detail": detail}
        try:
            sdn: SDNClient = ctx.request_context.lifespan_context["sdn_client"]
            if not sdn.configured:
                return skeleton_payload()
            result = await sdn.query_devices(
                name=name, page_num=page_num, page_size=page_size
            )
            return {"ok": True, "configured": True, **result.model_dump(by_alias=True)}
        except SDNError as exc:
            await ctx.error(f"sdn_device_by_name failed: {exc}")
            return {"ok": False, "configured": True, "detail": str(exc)}
        except Exception:
            logger.exception("sdn_device_by_name unexpected failure")
            return unexpected_payload()

    @mcp.tool(
        name="sdn_device_by_management_ip",
        description=(
            "Look up an SDN device by management IP (paged; page_num is 1-based). "
            "Returns {ok, configured, content[], total_elements, ...}. "
            "Device output never includes credentials (password/community)."
        ),
    )
    async def sdn_device_by_management_ip(
        ctx: Context[Any, Any, Any],
        management_ip: str,
        page_num: int = 1,
        page_size: int = 10,
    ) -> dict[str, object]:
        management_ip = management_ip.strip()
        if not management_ip:
            return {"ok": False, "detail": "management_ip must not be empty"}
        if detail := page_bounds_detail(page_num, page_size):
            return {"ok": False, "detail": detail}
        try:
            sdn: SDNClient = ctx.request_context.lifespan_context["sdn_client"]
            if not sdn.configured:
                return skeleton_payload()
            result = await sdn.query_devices(
                management_ip=management_ip, page_num=page_num, page_size=page_size
            )
            return {"ok": True, "configured": True, **result.model_dump(by_alias=True)}
        except SDNError as exc:
            await ctx.error(f"sdn_device_by_management_ip failed: {exc}")
            return {"ok": False, "configured": True, "detail": str(exc)}
        except Exception:
            logger.exception("sdn_device_by_management_ip unexpected failure")
            return unexpected_payload()
