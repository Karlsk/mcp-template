"""Device configuration diff tool (placeholder).

``get_config_diff`` will diff two controller configuration snapshots taken inside
the fault window (change analysis first). The snapshot store is not wired yet.
"""

from __future__ import annotations

from mcp.server.fastmcp import FastMCP

from app.tools.validation import not_implemented_payload, time_window_detail


def register(mcp: FastMCP) -> None:
    """Register the configuration diff tool."""

    @mcp.tool(
        name="get_config_diff",
        description=(
            "Diff a device's running configuration between two points inside the "
            "fault window (change analysis first). NOT IMPLEMENTED YET: returns "
            "{ok: false, configured: false}."
        ),
    )
    async def get_config_diff(
        device_name: str,
        start_time: str | None = None,
        end_time: str | None = None,
        section: str | None = None,
    ) -> dict[str, object]:
        if not device_name.strip():
            return {"ok": False, "detail": "device_name must not be empty"}
        if detail := time_window_detail(start_time, end_time):
            return {"ok": False, "detail": detail}
        return not_implemented_payload("controller config snapshots + graph")
