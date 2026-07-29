"""Performance statistics tools (§3.2 / §3.3 / §3.4).

Thin adapters over the history client methods. Each tool presets the metric
names for its domain and a default last-hour window (overridable via
start_time/end_time). period is a Literal enum, so invalid values are rejected
by the MCP schema (isError) rather than the tool body.
"""

from __future__ import annotations

import logging
from typing import Any

from mcp.server.fastmcp import Context, FastMCP

from app.sdn import SDNClient, SDNError
from app.sdn.client import PerfPeriod
from app.tools.validation import (
    skeleton_payload,
    time_window_detail,
    unexpected_payload,
)

logger = logging.getLogger(__name__)

PORT_TRAFFIC_METRICS = ("in_traffic", "out_traffic")
LINK_PERF_METRICS = ("jitter", "rtt", "loss")
TRAFFIC_AND_PKG_METRICS = ("in_traffic", "out_traffic", "in_pkg", "out_pkg")


def register(mcp: FastMCP) -> None:
    """Register performance statistics tools."""

    @mcp.tool(
        name="sdn_port_traffic",
        description=(
            "Port traffic history (in/out) for a device + port over the last hour "
            "(or a custom start_time/end_time window). period ∈ 5m/1h/1d/1M."
        ),
    )
    async def sdn_port_traffic(
        ctx: Context[Any, Any, Any],
        device_name: str,
        port_name: str,
        period: PerfPeriod = "5m",
        start_time: str | None = None,
        end_time: str | None = None,
    ) -> dict[str, object]:
        device_name = device_name.strip()
        port_name = port_name.strip()
        if not device_name or not port_name:
            return {"ok": False, "detail": "device_name and port_name must not be empty"}
        if detail := time_window_detail(start_time, end_time):
            return {"ok": False, "detail": detail}
        try:
            sdn: SDNClient = ctx.request_context.lifespan_context["sdn_client"]
            if not sdn.configured:
                return skeleton_payload()
            result = await sdn.query_switch_history(
                "port",
                metric_names=PORT_TRAFFIC_METRICS,
                device_name=device_name,
                port_name=port_name,
                period=period,
                start_time=start_time,
                end_time=end_time,
            )
            return {"ok": True, "configured": True, **result.model_dump(by_alias=True)}
        except SDNError as exc:
            await ctx.error(f"sdn_port_traffic failed: {exc}")
            return {"ok": False, "configured": True, "detail": str(exc)}
        except Exception:
            logger.exception("sdn_port_traffic unexpected failure")
            return unexpected_payload()

    @mcp.tool(
        name="sdn_link_performance",
        description=(
            "Link performance history (jitter, delay/rtt, loss) for a linkId over "
            "the last hour (or a custom window). period ∈ 5m/1h/1d/1M."
        ),
    )
    async def sdn_link_performance(
        ctx: Context[Any, Any, Any],
        link_id: str,
        period: PerfPeriod = "5m",
        start_time: str | None = None,
        end_time: str | None = None,
    ) -> dict[str, object]:
        link_id = link_id.strip()
        if not link_id:
            return {"ok": False, "detail": "link_id must not be empty"}
        if detail := time_window_detail(start_time, end_time):
            return {"ok": False, "detail": detail}
        try:
            sdn: SDNClient = ctx.request_context.lifespan_context["sdn_client"]
            if not sdn.configured:
                return skeleton_payload()
            result = await sdn.query_switch_history(
                "link",
                metric_names=LINK_PERF_METRICS,
                link_id=link_id,
                period=period,
                start_time=start_time,
                end_time=end_time,
            )
            return {"ok": True, "configured": True, **result.model_dump(by_alias=True)}
        except SDNError as exc:
            await ctx.error(f"sdn_link_performance failed: {exc}")
            return {"ok": False, "configured": True, "detail": str(exc)}
        except Exception:
            logger.exception("sdn_link_performance unexpected failure")
            return unexpected_payload()

    @mcp.tool(
        name="sdn_vpn_traffic",
        description=(
            "VPN traffic history (in/out traffic, in/out pkg) for a vpnId over the "
            "last hour (or a custom window). period ∈ 5m/1h/1d/1M."
        ),
    )
    async def sdn_vpn_traffic(
        ctx: Context[Any, Any, Any],
        vpn_id: str,
        period: PerfPeriod = "5m",
        start_time: str | None = None,
        end_time: str | None = None,
    ) -> dict[str, object]:
        vpn_id = vpn_id.strip()
        if not vpn_id:
            return {"ok": False, "detail": "vpn_id must not be empty"}
        if detail := time_window_detail(start_time, end_time):
            return {"ok": False, "detail": detail}
        try:
            sdn: SDNClient = ctx.request_context.lifespan_context["sdn_client"]
            if not sdn.configured:
                return skeleton_payload()
            result = await sdn.query_vpn_history(
                vpn_id,
                metric_names=TRAFFIC_AND_PKG_METRICS,
                period=period,
                start_time=start_time,
                end_time=end_time,
            )
            return {"ok": True, "configured": True, **result.model_dump(by_alias=True)}
        except SDNError as exc:
            await ctx.error(f"sdn_vpn_traffic failed: {exc}")
            return {"ok": False, "configured": True, "detail": str(exc)}
        except Exception:
            logger.exception("sdn_vpn_traffic unexpected failure")
            return unexpected_payload()

    @mcp.tool(
        name="sdn_te_tunnel_traffic",
        description=(
            "TE tunnel traffic history (in/out traffic, in/out pkg) for a device + "
            "tunnel over the last hour (or a custom window). period ∈ 5m/1h/1d/1M."
        ),
    )
    async def sdn_te_tunnel_traffic(
        ctx: Context[Any, Any, Any],
        device_name: str,
        tunnel_name: str,
        period: PerfPeriod = "5m",
        start_time: str | None = None,
        end_time: str | None = None,
    ) -> dict[str, object]:
        device_name = device_name.strip()
        tunnel_name = tunnel_name.strip()
        if not device_name or not tunnel_name:
            return {"ok": False, "detail": "device_name and tunnel_name must not be empty"}
        if detail := time_window_detail(start_time, end_time):
            return {"ok": False, "detail": detail}
        try:
            sdn: SDNClient = ctx.request_context.lifespan_context["sdn_client"]
            if not sdn.configured:
                return skeleton_payload()
            result = await sdn.query_te_history(
                device_name,
                tunnel_name,
                metric_names=TRAFFIC_AND_PKG_METRICS,
                period=period,
                start_time=start_time,
                end_time=end_time,
            )
            return {"ok": True, "configured": True, **result.model_dump(by_alias=True)}
        except SDNError as exc:
            await ctx.error(f"sdn_te_tunnel_traffic failed: {exc}")
            return {"ok": False, "configured": True, "detail": str(exc)}
        except Exception:
            logger.exception("sdn_te_tunnel_traffic unexpected failure")
            return unexpected_payload()
