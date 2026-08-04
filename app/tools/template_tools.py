"""Command template library search tool (placeholder).

``search_command_template`` will run controlled retrieval over the command
template library (YAML) by intent x vendor. The library is not wired yet;
spec-04 replaces the function body while keeping this signature frozen. See
docs/spec-01-工具占位与注册面.md.
"""

from __future__ import annotations

from mcp.server.fastmcp import FastMCP

from app.tools.validation import not_implemented_payload


def register(mcp: FastMCP) -> None:
    """Register the command template search tool."""

    @mcp.tool(
        name="search_command_template",
        description=(
            "Controlled retrieval of device command templates from the command "
            "template library (YAML) by action/intent x vendor. NOT IMPLEMENTED "
            "YET: returns {ok: false, configured: false}."
        ),
    )
    async def search_command_template(
        action: str | None = None,
        vendor: str | None = None,
        keyword: str | None = None,
    ) -> dict[str, object]:
        return not_implemented_payload("command template library (YAML)")
