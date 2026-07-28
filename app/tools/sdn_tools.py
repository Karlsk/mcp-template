"""SDN query tools — thin MCP adapters over :class:`SDNClient`.

Each tool only:
1. resolves the ``SDNClient`` from the lifespan context,
2. applies parameter presets (defaults) and validates parameter ranges,
3. calls ONE client method (all business logic lives in the client), and
4. translates the typed result / sanitized error into a structured dict.

No endpoint URLs, request bodies, or response parsing here. Every tool keeps a
final ``except Exception`` so raw exceptions never reach MCP's error path (which
would serialize ``str()`` — potentially URLs/credentials — into the ``isError``
text field).
"""

from __future__ import annotations

import logging
from typing import Any

from mcp.server.fastmcp import Context, FastMCP

from app.sdn import SDNClient, SDNError

logger = logging.getLogger(__name__)

MAX_PAGE_SIZE = 100


def register(mcp: FastMCP) -> None:
    """Register SDN query tools."""

    @mcp.tool(
        name="sdn_alerts",
        description=(
            "Query paged device alerts from the SDN controller. Optional filters: "
            "interval (e.g. '1h'), namespace, category; plus pagination. "
            "Returns {ok, configured, total, success, message, code, data}."
        ),
    )
    async def sdn_alerts(
        ctx: Context[Any, Any, Any],
        interval: str = "1h",
        namespace: str = "device",
        category: str = "PE端口Down",
        page_num: int = 1,
        page_size: int = 10,
    ) -> dict[str, object]:
        # Parameter presets are the defaults above; validate ranges here. These
        # messages describe caller input only, so they are safe to surface.
        if page_num < 1:
            return {"ok": False, "detail": f"page_num must be >= 1 (got {page_num})"}
        if not 1 <= page_size <= MAX_PAGE_SIZE:
            return {
                "ok": False,
                "detail": f"page_size must be in [1, {MAX_PAGE_SIZE}] (got {page_size})",
            }
        try:
            sdn: SDNClient = ctx.request_context.lifespan_context["sdn_client"]
            if not sdn.configured:
                return {
                    "ok": False,
                    "configured": False,
                    "detail": "SDN controller not configured (skeleton mode).",
                }
            result = await sdn.query_alerts(
                interval=interval,
                namespace=namespace,
                category=category,
                page_num=page_num,
                page_size=page_size,
            )
            return {"ok": True, "configured": True, **result.model_dump()}
        except SDNError as exc:
            # Safe public message; raw detail stays in server logs only.
            await ctx.error(f"sdn_alerts failed: {exc}")
            return {"ok": False, "configured": True, "detail": str(exc)}
        except Exception:
            # Last line of defense: never let a raw exception reach MCP's error path.
            logger.exception("sdn_alerts unexpected failure")
            return {"ok": False, "configured": False, "detail": "Unexpected server error."}
