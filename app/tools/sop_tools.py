"""SOP graph retrieval tool (placeholder).

``search_sop`` will run controlled retrieval over the SOP graph (Neo4j) by
fault type / intent. The graph store is not wired yet; spec-03 replaces the
function body while keeping this signature frozen. See
docs/spec-01-工具占位与注册面.md.
"""

from __future__ import annotations

from mcp.server.fastmcp import FastMCP

from app.tools.validation import not_implemented_payload


def register(mcp: FastMCP) -> None:
    """Register the SOP search tool."""

    @mcp.tool(
        name="search_sop",
        description=(
            "Controlled retrieval of SOPs from the SOP graph (Neo4j) by fault "
            "type / intent / keyword. NOT IMPLEMENTED YET: returns {ok: false, "
            "configured: false}."
        ),
    )
    async def search_sop(
        fault_type: str | None = None,
        intent: str | None = None,
        keyword: str | None = None,
        db: str | None = None,
        event_id: str | None = None,
        limit: int = 10,
        max_depth: int = 20,  # == graph.cypher.MAX_SOP_DEPTH (spec-03 §4.5)
    ) -> dict[str, object]:
        return not_implemented_payload("SOP graph (Neo4j)")
