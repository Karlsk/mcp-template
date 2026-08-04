"""Change-event history tool (placeholder).

``get_change_history`` will read change events from PostgreSQL for the fault
window. The PG event source is not wired yet.
"""

from __future__ import annotations

from mcp.server.fastmcp import FastMCP

from app.tools.validation import (
    not_implemented_payload,
    page_bounds_detail,
    time_window_detail,
)


def register(mcp: FastMCP) -> None:
    """Register the change-event history tool."""

    @mcp.tool(
        name="get_change_history",
        description=(
            "List change events (config pushes, maintenance, upgrades) recorded "
            "inside the fault window, optionally scoped to one device. page_num is "
            "1-based. NOT IMPLEMENTED YET: returns {ok: false, configured: false}."
        ),
    )
    async def get_change_history(
        device_name: str | None = None,
        start_time: str | None = None,
        end_time: str | None = None,
        page_num: int = 1,
        page_size: int = 10,
    ) -> dict[str, object]:
        if detail := page_bounds_detail(page_num, page_size):
            return {"ok": False, "detail": detail}
        if detail := time_window_detail(start_time, end_time):
            return {"ok": False, "detail": detail}
        return not_implemented_payload("PostgreSQL change events")
