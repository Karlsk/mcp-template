"""Command template library lookup tool.

``search_command_template`` serves commands from the curated YAML library
(``app/templates``); the server never invents or renders a command. Parameter
names stay frozen from spec-01; only the body changed. See
docs/spec-04-命令模板库与search_command_template.md.
"""

from __future__ import annotations

import logging
from typing import Any

from mcp.server.fastmcp import Context, FastMCP

from app.templates import (
    CommandTemplateRegistry,
    TemplateError,
    get_registry,
    normalize_vendor,
)
from app.templates.registry import MAX_TEMPLATE_RESULTS
from app.tools.validation import unexpected_payload

logger = logging.getLogger(__name__)


def _unknown_action_payload(
    registry: CommandTemplateRegistry, action: str, vendor: str | None
) -> dict[str, object]:
    """Unknown action: tell the model what IS available so it can self-correct."""
    available = registry.actions[:MAX_TEMPLATE_RESULTS]
    detail = f"unknown action '{action}'"
    if vendor:
        detail += f" (vendor '{normalize_vendor(vendor)}')"
        if action in registry.actions:
            detail += f"; no template for vendor '{normalize_vendor(vendor)}' and no default entry"
    return {
        "ok": False,
        "detail": detail,
        "available_actions": available,
        "truncated": len(registry.actions) > len(available),
    }


def register(mcp: FastMCP) -> None:
    """Register the command template lookup tool."""

    @mcp.tool(
        name="search_command_template",
        description=(
            "Look up the device CLI command for an abstract action (intent) on a "
            "given vendor. Feed it a SOP step's 'action' plus the device vendor. "
            "action+vendor returns one template; action alone returns every vendor "
            "variant; vendor or keyword alone lists matching actions; no argument "
            "lists all actions. Vendor spellings are normalized (hw/vrp->huawei, "
            "hpe/comware->h3c, ios/nxos->cisco) and fall back to the 'default' "
            "entry. Placeholders like {interface} are returned in 'placeholders' "
            "for the caller to fill — the server does not render them. Commands "
            "come from a curated library only; nothing is generated. Returns "
            "{ok, mode, template|templates|actions}."
        ),
    )
    async def search_command_template(
        ctx: Context[Any, Any, Any],
        action: str | None = None,
        vendor: str | None = None,
        keyword: str | None = None,
    ) -> dict[str, object]:
        try:
            registry = get_registry()

            # 1. action + vendor -> exactly one template (O(1))
            if action and vendor:
                template = registry.get(action, vendor)
                if template is None:
                    return _unknown_action_payload(registry, action, vendor)
                return {"ok": True, "mode": "template",
                        "template": template.model_dump()}

            # 2. action only -> every vendor variant
            if action:
                templates = registry.get_all_vendors(action)
                if not templates:
                    return _unknown_action_payload(registry, action, None)
                return {"ok": True, "mode": "templates",
                        "action": action,
                        "templates": [t.model_dump() for t in templates]}

            # 3/4. vendor and/or keyword -> action list; no argument -> all actions
            summaries = registry.search(keyword=keyword, vendor=vendor)
            return {"ok": True, "mode": "actions",
                    "actions": [s.model_dump() for s in summaries],
                    "total": len(registry.actions),
                    "truncated": len(summaries) >= MAX_TEMPLATE_RESULTS}
        except TemplateError as exc:
            await ctx.error(f"search_command_template failed: {exc}")
            return {"ok": False, "detail": str(exc)}
        except Exception:
            logger.exception("search_command_template unexpected failure")
            return unexpected_payload()
