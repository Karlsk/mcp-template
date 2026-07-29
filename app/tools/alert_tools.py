"""Alert query tool (§3.5) — multi-condition alerts, preset to one device / last hour.

Thin adapter over :class:`SDNClient.query_alert_page` (the v1.5 contract). The
legacy ``sdn_alerts`` tool in ``sdn_tools.py`` is intentionally left untouched.
"""

from __future__ import annotations

import logging
from typing import Any

from mcp.server.fastmcp import Context, FastMCP

from app.sdn import SDNClient, SDNError
from app.sdn.client import AlertLevel
from app.tools.validation import (
    page_bounds_detail,
    skeleton_payload,
    time_window_detail,
    unexpected_payload,
)

logger = logging.getLogger(__name__)

_AUTO_RECOVERY_VALUES = {1, 2, 3}


def register(mcp: FastMCP) -> None:
    """Register the alert query tool."""

    @mcp.tool(
        name="sdn_device_alerts",
        description=(
            "Query controller alerts with multi-condition filters, defaulting to a "
            "single device's alerts over the last hour. device_name maps to the "
            "alert source list; pass None for all sources. Optional filters: "
            "category, level (CRITICAL/MAJOR/MINOR/WARNING), auto_recovery "
            "(1=unrecovered, 2=auto, 3=manual), msg (fuzzy). page_num is 1-based. "
            "Returns {ok, configured, total, code, message, data[]}."
        ),
    )
    async def sdn_device_alerts(
        ctx: Context[Any, Any, Any],
        device_name: str | None = None,
        start_time: str | None = None,
        end_time: str | None = None,
        namespace: str | None = None,
        category: str | None = None,
        level: AlertLevel | None = None,
        auto_recovery: int | None = None,
        msg: str | None = None,
        page_num: int = 1,
        page_size: int = 10,
    ) -> dict[str, object]:
        if detail := page_bounds_detail(page_num, page_size):
            return {"ok": False, "detail": detail}
        if detail := time_window_detail(start_time, end_time):
            return {"ok": False, "detail": detail}
        if auto_recovery is not None and auto_recovery not in _AUTO_RECOVERY_VALUES:
            return {
                "ok": False,
                "detail": f"auto_recovery must be one of {sorted(_AUTO_RECOVERY_VALUES)}",
            }
        source = device_name.strip() if device_name else None
        try:
            sdn: SDNClient = ctx.request_context.lifespan_context["sdn_client"]
            if not sdn.configured:
                return skeleton_payload()
            result = await sdn.query_alert_page(
                start_time=start_time,
                end_time=end_time,
                namespace=namespace,
                category=category,
                source_list=[source] if source else None,
                auto_recovery=auto_recovery,  # type: ignore[arg-type]
                msg=msg,
                level=level,
                page_num=page_num,
                page_size=page_size,
            )
            return {"ok": True, "configured": True, **result.model_dump(by_alias=True)}
        except SDNError as exc:
            await ctx.error(f"sdn_device_alerts failed: {exc}")
            return {"ok": False, "configured": True, "detail": str(exc)}
        except Exception:
            logger.exception("sdn_device_alerts unexpected failure")
            return unexpected_payload()
