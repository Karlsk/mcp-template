"""Device CLI command tool — run a command on a device and return its output.

Thin adapter over :class:`SDNClient.run_command` (POST
``/api/no/config/device-conf/command-result``).

Read-only by default: only diagnostic commands are allowed unless the caller
passes ``allow_write=True``. This is a soft guard (it inspects the command's
first token) — an accident-prevention rail, not a hard security boundary.
"""

from __future__ import annotations

import logging
from typing import Any

from mcp.server.fastmcp import Context, FastMCP

from app.sdn import SDNClient, SDNError
from app.tools.validation import skeleton_payload, unexpected_payload

logger = logging.getLogger(__name__)

# Commands that only inspect the device. Matched against the first token
# (case-insensitive). Not exhaustive; extend as needed.
_READONLY_PREFIXES = (
    "display",
    "dis",
    "show",
    "ping",
    "tracert",
    "traceroute",
    "pwd",
    "dir",
    "ls",
)


def _is_readonly_command(command: str) -> bool:
    """True if the command's first token looks like a diagnostic/read-only verb."""
    tokens = command.strip().lower().split()
    if not tokens:
        return False
    head = tokens[0]
    return head in _READONLY_PREFIXES or head.startswith(_READONLY_PREFIXES)


def register(mcp: FastMCP) -> None:
    """Register the device command tool."""

    @mcp.tool(
        name="sdn_run_command",
        description=(
            "Run a CLI command on an SDN device and return the textual output "
            "(e.g. 'display version', 'dis ip in br'). READ-ONLY BY DEFAULT: only "
            "diagnostic commands (display/dis/show/ping/tracert/...) are allowed; "
            "set allow_write=True to run any command (config changes, reboot, ...) "
            "— use with care. Returns {ok, configured, result}."
        ),
    )
    async def sdn_run_command(
        ctx: Context[Any, Any, Any],
        device_name: str,
        command: str,
        allow_write: bool = False,
    ) -> dict[str, object]:
        device_name = device_name.strip()
        command = command.strip()
        if not device_name or not command:
            return {"ok": False, "detail": "device_name and command must not be empty"}
        if not allow_write and not _is_readonly_command(command):
            return {
                "ok": False,
                "detail": (
                    "command is not recognized as read-only (display/dis/show/ping/...); "
                    "this tool is read-only by default to avoid accidental changes. "
                    "Re-issue with allow_write=True to execute it."
                ),
            }
        try:
            sdn: SDNClient = ctx.request_context.lifespan_context["sdn_client"]
            if not sdn.configured:
                return skeleton_payload()
            result = await sdn.run_command(device_name, command)
            return {"ok": True, "configured": True, **result.model_dump()}
        except SDNError as exc:
            await ctx.error(f"sdn_run_command failed: {exc}")
            return {"ok": False, "configured": True, "detail": str(exc)}
        except Exception:
            logger.exception("sdn_run_command unexpected failure")
            return unexpected_payload()
