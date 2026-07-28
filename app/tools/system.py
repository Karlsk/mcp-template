"""System tools: server liveness probe and SDN connectivity health check.

These tools demonstrate the two patterns used across the server:
- ``ping``: a pure tool with no external dependency (proves registration works).
- ``sdn_health``: a tool that uses the lifespan-owned :class:`SDNClient` and
  converts every error into a sanitized structured dict — never raising into the
  MCP error path. MCP's low-level handler serializes any uncaught exception's
  ``str()`` into an ``isError`` text message to the model (httpx strings embed
  URLs/status codes), so the tool boundary must catch *everything*: ``SDNError``
  for mapped HTTP/transport failures, plus a final ``except Exception`` for the
  unexpected (lifespan wiring bugs, response validation errors, ...).
"""

from __future__ import annotations

import logging
from typing import Any

from mcp.server.fastmcp import Context, FastMCP

from app.sdn import SDNClient, SDNError

logger = logging.getLogger(__name__)


def register(mcp: FastMCP) -> None:
    """Register system tools on the given FastMCP server."""

    @mcp.tool(
        name="ping",
        description="Echo back a message to verify the MCP server is reachable.",
    )
    async def ping(message: str = "ping") -> str:
        return f"pong: {message}"

    @mcp.tool(
        name="sdn_health",
        description=(
            "Check connectivity to the SDN controller. "
            "Returns a structured status dict "
            "{ok, configured, detail?, status_code?, latency_ms?}."
        ),
    )
    async def sdn_health(ctx: Context[Any, Any, Any]) -> dict[str, object]:
        try:
            sdn: SDNClient = ctx.request_context.lifespan_context["sdn_client"]
            if not sdn.configured:
                return {
                    "ok": False,
                    "configured": False,
                    "detail": "SDN controller not configured (skeleton mode).",
                }
            result = await sdn.health()
            return {"ok": True, "configured": True, **result.model_dump()}
        except SDNError as exc:
            # Safe public message; raw detail stays in server logs only.
            await ctx.error(f"sdn_health failed: {exc}")
            return {"ok": False, "configured": True, "detail": str(exc)}
        except Exception:
            # Last line of defense: never let a raw exception reach MCP's error
            # path, which would serialize str(exc) to the model. Log fully
            # server-side; return only a generic message.
            logger.exception("sdn_health unexpected failure")
            return {"ok": False, "configured": False, "detail": "Unexpected server error."}
