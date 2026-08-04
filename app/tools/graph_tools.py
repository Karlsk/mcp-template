"""Graph-backed topology tools (placeholders).

``get_fault_subgraph`` and ``get_topology_snapshot`` will read the topology
snapshot graph (and alerts, for the fault subgraph). Neither data source is wired
yet, so both tools are registered with their final signature and return the
``not_implemented`` envelope. See docs/spec-01-工具占位与注册面.md.
"""

from __future__ import annotations

from mcp.server.fastmcp import FastMCP

from app.tools.validation import not_implemented_payload, time_window_detail


def register(mcp: FastMCP) -> None:
    """Register the graph topology tools."""

    @mcp.tool(
        name="get_fault_subgraph",
        description=(
            "Build the fault subgraph for a device/fault: the topology neighbourhood "
            "around the seed device plus the alerts raised inside the fault window. "
            "Deterministic by construction — the caller only supplies the device and "
            "fault parameters. NOT IMPLEMENTED YET: returns {ok: false, "
            "configured: false}."
        ),
    )
    async def get_fault_subgraph(
        device_name: str,
        fault_type: str | None = None,
        start_time: str | None = None,
        end_time: str | None = None,
        hops: int = 1,
    ) -> dict[str, object]:
        if not device_name.strip():
            return {"ok": False, "detail": "device_name must not be empty"}
        if detail := time_window_detail(start_time, end_time):
            return {"ok": False, "detail": detail}
        if not 1 <= hops <= 3:
            return {"ok": False, "detail": f"hops must be in [1, 3] (got {hops})"}
        return not_implemented_payload("topology snapshot graph + alerts")

    @mcp.tool(
        name="get_topology_snapshot",
        description=(
            "Replay the topology as it was at a point in time (graph snapshot). "
            "NOT IMPLEMENTED YET: returns {ok: false, configured: false}."
        ),
    )
    async def get_topology_snapshot(
        at_time: str | None = None,
        device_name: str | None = None,
    ) -> dict[str, object]:
        return not_implemented_payload("topology snapshot graph")
