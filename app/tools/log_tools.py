"""System operation log tool (§3.12).

Thin adapter over :class:`SDNClient.query_operation_logs`.
"""

from __future__ import annotations

import logging
from typing import Any

from mcp.server.fastmcp import Context, FastMCP

from app.sdn import SDNClient, SDNError
from app.tools.validation import (
    page_bounds_detail,
    skeleton_payload,
    time_window_detail,
    unexpected_payload,
)

logger = logging.getLogger(__name__)


def register(mcp: FastMCP) -> None:
    """Register the operation log tool."""

    @mcp.tool(
        name="sdn_operation_logs",
        description=(
            "Page through controller system operation logs over the last hour "
            "(or a custom start_time/end_time window). page_num is 1-based. "
            "Returns {ok, configured, code, message, data[]}."
        ),
    )
    async def sdn_operation_logs(
        ctx: Context[Any, Any, Any],
        start_time: str | None = None,
        end_time: str | None = None,
        page_num: int = 1,
        page_size: int = 10,
    ) -> dict[str, object]:
        if detail := page_bounds_detail(page_num, page_size):
            return {"ok": False, "detail": detail}
        if detail := time_window_detail(start_time, end_time):
            return {"ok": False, "detail": detail}
        try:
            sdn: SDNClient = ctx.request_context.lifespan_context["sdn_client"]
            if not sdn.configured:
                return skeleton_payload()
            result = await sdn.query_operation_logs(
                start_time=start_time,
                end_time=end_time,
                page_num=page_num,
                page_size=page_size,
            )
            return {"ok": True, "configured": True, **result.model_dump(by_alias=True)}
        except SDNError as exc:
            await ctx.error(f"sdn_operation_logs failed: {exc}")
            return {"ok": False, "configured": True, "detail": str(exc)}
        except Exception:
            logger.exception("sdn_operation_logs unexpected failure")
            return unexpected_payload()
