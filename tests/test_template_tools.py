"""Tool-layer tests for search_command_template (spec-04 §7, tool items 1-6).

Runs over in-memory MCP sessions; the process-wide registry is pointed at a tmp
YAML via COMMAND_TEMPLATE_FILE so no test touches the seed library.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
import yaml
from mcp.types import TextContent

from app.settings import Settings
from app.templates import reset_registry
from app.tools import template_tools

TMP_TEMPLATES: dict[str, Any] = {
    "verify_interface_state": {
        "name": "检查接口状态",
        "observation": "oper_state",
        "vendors": {
            "huawei": {"command": "display interface {interface} brief"},
            "cisco": {"command": "show interface {interface} status"},
            "default": {"command": "show interface {interface}"},
        },
    },
    "check_route": {
        "name": "检查路由表项",
        "observation": "next_hop",
        "vendors": {
            # Deliberately no `default`: exercises the §4.3 third branch.
            "huawei": {"command": "display ip routing-table {prefix}"},
        },
    },
}


def _payload(result: Any) -> dict[str, Any]:
    if getattr(result, "structuredContent", None):
        return dict(result.structuredContent)
    block = result.content[0]
    assert isinstance(block, TextContent), f"unexpected content: {block!r}"
    return json.loads(block.text)


@pytest.fixture
def template_env(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    unconfigured_settings: Settings,
) -> Iterator[Settings]:
    """Point the process-wide registry at a tmp YAML for this test only."""
    path = tmp_path / "templates.yaml"
    payload = yaml.safe_dump({"templates": TMP_TEMPLATES}, sort_keys=False, allow_unicode=True)
    path.write_text(payload, encoding="utf-8")
    monkeypatch.setenv("COMMAND_TEMPLATE_FILE", str(path))
    reset_registry()
    yield unconfigured_settings
    reset_registry()


# ---------------------------------------------------------------------------
# §7.1 the four retrieval modes
# ---------------------------------------------------------------------------


async def test_action_plus_vendor_returns_one_template(make_session, template_env) -> None:
    async with make_session(template_env) as session:
        await session.initialize()
        result = await session.call_tool(
            "search_command_template",
            {"action": "verify_interface_state", "vendor": "hw"},
        )
    payload = _payload(result)
    assert payload["ok"] is True
    assert payload["mode"] == "template"
    template = payload["template"]
    assert template["vendor"] == "huawei"  # normalized spelling
    assert template["command"] == "display interface {interface} brief"
    assert template["placeholders"] == ["interface"]
    assert template["fallback"] is None


async def test_action_alone_returns_every_vendor_variant(
    make_session, template_env
) -> None:
    async with make_session(template_env) as session:
        await session.initialize()
        result = await session.call_tool(
            "search_command_template", {"action": "verify_interface_state"}
        )
    payload = _payload(result)
    assert payload["ok"] is True
    assert payload["mode"] == "templates"
    assert payload["action"] == "verify_interface_state"
    assert [t["vendor"] for t in payload["templates"]] == ["huawei", "cisco", "default"]


async def test_keyword_alone_lists_matching_actions(make_session, template_env) -> None:
    async with make_session(template_env) as session:
        await session.initialize()
        result = await session.call_tool(
            "search_command_template", {"keyword": "接口"}
        )
    payload = _payload(result)
    assert payload["ok"] is True
    assert payload["mode"] == "actions"
    assert [a["action"] for a in payload["actions"]] == ["verify_interface_state"]
    assert payload["total"] == 2
    assert payload["truncated"] is False


async def test_no_argument_lists_all_actions(make_session, template_env) -> None:
    async with make_session(template_env) as session:
        await session.initialize()
        result = await session.call_tool("search_command_template", {})
    payload = _payload(result)
    assert payload["ok"] is True
    assert payload["mode"] == "actions"
    assert [a["action"] for a in payload["actions"]] == [
        "verify_interface_state",
        "check_route",
    ]
    assert payload["total"] == 2


# ---------------------------------------------------------------------------
# §7.2-3 unknown action self-correction
# ---------------------------------------------------------------------------


async def test_unknown_action_offers_available_actions(make_session, template_env) -> None:
    async with make_session(template_env) as session:
        await session.initialize()
        result = await session.call_tool(
            "search_command_template", {"action": "check_intf_err", "vendor": "huawei"}
        )
    payload = _payload(result)
    assert payload["ok"] is False
    assert "check_intf_err" in str(payload["detail"])
    assert payload["available_actions"] == ["verify_interface_state", "check_route"]
    assert payload["truncated"] is False


async def test_vendor_gap_without_default_is_explained(
    make_session, template_env
) -> None:
    async with make_session(template_env) as session:
        await session.initialize()
        result = await session.call_tool(
            "search_command_template", {"action": "check_route", "vendor": "cisco"}
        )
    payload = _payload(result)
    assert payload["ok"] is False
    assert "no default entry" in str(payload["detail"])
    assert "cisco" in str(payload["detail"])


# ---------------------------------------------------------------------------
# §7.4-5 error paths
# ---------------------------------------------------------------------------


async def test_template_error_is_surfaced_safely(
    make_session, template_env, monkeypatch: pytest.MonkeyPatch
) -> None:
    from app.templates import TemplateError

    def broken() -> Any:
        raise TemplateError("command template library is not valid YAML: boom")

    monkeypatch.setattr(template_tools, "get_registry", broken)
    async with make_session(template_env) as session:
        await session.initialize()
        result = await session.call_tool(
            "search_command_template", {"action": "check_route"}
        )
    payload = _payload(result)
    assert payload["ok"] is False
    assert payload["detail"] == "command template library is not valid YAML: boom"


async def test_unexpected_error_returns_generic_envelope(
    make_session, template_env, monkeypatch: pytest.MonkeyPatch
) -> None:
    def broken() -> Any:
        raise RuntimeError("boom")

    monkeypatch.setattr(template_tools, "get_registry", broken)
    async with make_session(template_env) as session:
        await session.initialize()
        result = await session.call_tool(
            "search_command_template", {"action": "check_route"}
        )
    payload = _payload(result)
    assert payload["ok"] is False
    assert payload["detail"] == "Unexpected server error."
    assert "boom" not in json.dumps(payload)


# ---------------------------------------------------------------------------
# §7.6 the safety-contract sentences in the description
# ---------------------------------------------------------------------------


async def test_description_states_the_safety_contract(
    make_session, template_env
) -> None:
    async with make_session(template_env) as session:
        await session.initialize()
        tools = (await session.list_tools()).tools
    tool = next(t for t in tools if t.name == "search_command_template")
    assert tool.description is not None
    assert "does not render" in tool.description
    assert "curated library" in tool.description
