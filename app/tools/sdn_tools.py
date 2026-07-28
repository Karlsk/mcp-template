"""SDN query tools.

Registered via :func:`app.tools.register_all`. Every tool must:
- Get ``SDNClient`` from ``ctx.request_context.lifespan_context["sdn_client"]``.
- Catch ``SDNError`` (sanitized message) and ``Exception`` (last-resort fallback)
  so raw exceptions never reach MCP's error path (which would serialize ``str()``
  containing URLs/credentials into the ``isError`` text field).
"""

from __future__ import annotations

import logging
from typing import Any

from mcp.server.fastmcp import Context, FastMCP

from app.sdn import SDNClient, SDNError

logger = logging.getLogger(__name__)


def register(mcp: FastMCP) -> None:
    """Register SDN query tools."""

    @mcp.tool(
        name="sdn_alerts",
        description=(
            "Query device alerts from the SDN controller. "
            "Returns structured result with total count and alert list."
        ),
    )
    async def sdn_alerts(ctx: Context[Any, Any, Any]) -> dict[str, object]:
        """POST /monitor/v2/alert/page with fixed body (PE端口Down, 1h, top 10)."""
        try:
            sdn: SDNClient = ctx.request_context.lifespan_context["sdn_client"]
            if not sdn.configured:
                return {
                    "ok": False,
                    "configured": False,
                    "detail": "SDN controller not configured (skeleton mode).",
                }
            endpoint = "/monitor/v2/alert/page"
            body = {
                "interval": "1h",
                "namespace": "device",
                "category": "PE端口Down",
                "pageNum": 1,
                "pageSize": 10,
            }
            resp = await sdn.request("POST", endpoint, json=body)
            data = resp.json()
            return {
                "ok": True,
                "configured": True,
                "status_code": resp.status_code,
                "total": data.get("total", 0),
                "success": data.get("success", False),
                "message": data.get("message", ""),
                "data": data.get("data", []),
            }
        except SDNError as exc:
            await ctx.error(f"sdn_alerts failed: {exc}")
            return {"ok": False, "configured": True, "detail": str(exc)}
        except Exception:
            logger.exception("sdn_alerts unexpected failure")
            return {"ok": False, "configured": False, "detail": "Unexpected server error."}

