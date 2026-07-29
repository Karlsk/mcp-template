"""Link query tool (§2.16) — look up links by id and optional filters.

Thin adapter over :class:`SDNClient.query_links`.
"""

from __future__ import annotations

import logging
from typing import Any

from mcp.server.fastmcp import Context, FastMCP

from app.sdn import SDNClient, SDNError
from app.sdn.client import LinkStatus, LinkType
from app.tools.validation import page_bounds_detail, skeleton_payload, unexpected_payload

logger = logging.getLogger(__name__)


def register(mcp: FastMCP) -> None:
    """Register the link query tool."""

    @mcp.tool(
        name="sdn_link_info",
        description=(
            "Query SDN links, optionally filtered by linkId and other fields "
            "(paged; page_num is 1-based, converted to the 0-based wire page). "
            "Returns {ok, configured, content[], total_elements, ...}."
        ),
    )
    async def sdn_link_info(
        ctx: Context[Any, Any, Any],
        page_num: int = 1,
        page_size: int = 10,
        link_id: str | None = None,
        source_node: str | None = None,
        destination: str | None = None,
        source_ip: str | None = None,
        destination_ip: str | None = None,
        status: LinkStatus | None = None,
        link_type: LinkType | None = None,
        label: str | None = None,
        label_filter_type: str | None = None,
    ) -> dict[str, object]:
        if detail := page_bounds_detail(page_num, page_size):
            return {"ok": False, "detail": detail}
        try:
            sdn: SDNClient = ctx.request_context.lifespan_context["sdn_client"]
            if not sdn.configured:
                return skeleton_payload()
            result = await sdn.query_links(
                page_num=page_num,
                page_size=page_size,
                link_id=link_id,
                source_node=source_node,
                destination=destination,
                source_ip=source_ip,
                destination_ip=destination_ip,
                status=status,
                link_type=link_type,
                label=label,
                label_filter_type=label_filter_type,
            )
            return {"ok": True, "configured": True, **result.model_dump(by_alias=True)}
        except SDNError as exc:
            await ctx.error(f"sdn_link_info failed: {exc}")
            return {"ok": False, "configured": True, "detail": str(exc)}
        except Exception:
            logger.exception("sdn_link_info unexpected failure")
            return unexpected_payload()
