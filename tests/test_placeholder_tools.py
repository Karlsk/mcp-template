"""Tests for the spec-01 placeholder tools and their validation helpers."""

from __future__ import annotations

import json
from typing import Any

import pytest
from mcp.types import TextContent

from app.settings import Settings
from app.tools.validation import (
    NOT_IMPLEMENTED_DETAIL,
    not_implemented_payload,
    positive_bound_detail,
)


def _payload(result: Any) -> dict[str, Any]:
    """Extract the structured dict a tool returned (structuredContent or text JSON)."""
    if getattr(result, "structuredContent", None):
        return dict(result.structuredContent)
    block = result.content[0]
    assert isinstance(block, TextContent), f"unexpected content: {block!r}"
    return json.loads(block.text)


# ---------------------------------------------------------------------------
# validation.py: not_implemented_payload / positive_bound_detail
# ---------------------------------------------------------------------------


def test_not_implemented_payload_without_hint() -> None:
    payload = not_implemented_payload()
    assert payload == {
        "ok": False,
        "configured": False,
        "detail": NOT_IMPLEMENTED_DETAIL,
    }
    assert "hint" not in payload


def test_not_implemented_payload_with_hint() -> None:
    payload = not_implemented_payload("topology snapshot graph")
    assert payload["ok"] is False
    assert payload["configured"] is False
    assert payload["detail"] == NOT_IMPLEMENTED_DETAIL
    assert payload["hint"] == "topology snapshot graph"


def test_positive_bound_detail_below_range() -> None:
    assert positive_bound_detail("hops", 0, 3) == "hops must be in [1, 3] (got 0)"


def test_positive_bound_detail_at_lower_bound() -> None:
    assert positive_bound_detail("hops", 1, 3) is None


def test_positive_bound_detail_above_range() -> None:
    assert positive_bound_detail("hops", 4, 3) == "hops must be in [1, 3] (got 4)"


# ---------------------------------------------------------------------------
# Placeholder tools over an in-memory MCP session
# ---------------------------------------------------------------------------

PLACEHOLDER_TOOLS = {
    "get_fault_subgraph",
    "get_topology_snapshot",
    "get_config_diff",
    "get_change_history",
}

PLACEHOLDER_HINTS = {
    "get_fault_subgraph": "topology snapshot graph + alerts",
    "get_topology_snapshot": "topology snapshot graph",
    "get_config_diff": "controller config snapshots + graph",
    "get_change_history": "PostgreSQL change events",
}

PLACEHOLDER_VALID_ARGS: dict[str, dict[str, object]] = {
    "get_fault_subgraph": {"device_name": "R1"},
    "get_topology_snapshot": {},
    "get_config_diff": {"device_name": "R1"},
    "get_change_history": {},
}


async def test_list_tools_includes_placeholders(
    make_session, unconfigured_settings: Settings
) -> None:
    async with make_session(unconfigured_settings) as session:
        await session.initialize()
        tools = (await session.list_tools()).tools
    names = {t.name for t in tools}
    assert names >= PLACEHOLDER_TOOLS
    for tool in tools:
        if tool.name in PLACEHOLDER_TOOLS:
            assert tool.description, f"{tool.name} must expose a description"


@pytest.mark.parametrize("name", sorted(PLACEHOLDER_TOOLS))
async def test_placeholder_returns_not_implemented(
    make_session, unconfigured_settings: Settings, name: str
) -> None:
    async with make_session(unconfigured_settings) as session:
        await session.initialize()
        result = await session.call_tool(name, PLACEHOLDER_VALID_ARGS[name])
    assert result.isError is False
    payload = _payload(result)
    assert payload["ok"] is False
    assert payload["configured"] is False
    assert payload["detail"] == NOT_IMPLEMENTED_DETAIL
    assert payload["hint"] == PLACEHOLDER_HINTS[name]


async def test_fault_subgraph_empty_device_name(
    make_session, unconfigured_settings: Settings
) -> None:
    async with make_session(unconfigured_settings) as session:
        await session.initialize()
        result = await session.call_tool("get_fault_subgraph", {"device_name": ""})
    payload = _payload(result)
    assert payload == {"ok": False, "detail": "device_name must not be empty"}


async def test_fault_subgraph_hops_out_of_range(
    make_session, unconfigured_settings: Settings
) -> None:
    async with make_session(unconfigured_settings) as session:
        await session.initialize()
        result = await session.call_tool(
            "get_fault_subgraph", {"device_name": "R1", "hops": 9}
        )
    payload = _payload(result)
    assert payload == {"ok": False, "detail": "hops must be in [1, 3] (got 9)"}


async def test_fault_subgraph_bad_time_window(
    make_session, unconfigured_settings: Settings
) -> None:
    async with make_session(unconfigured_settings) as session:
        await session.initialize()
        result = await session.call_tool(
            "get_fault_subgraph", {"device_name": "R1", "start_time": "bad"}
        )
    payload = _payload(result)
    assert payload == {
        "ok": False,
        "detail": "start_time and end_time must be provided together",
    }


async def test_config_diff_empty_device_name(
    make_session, unconfigured_settings: Settings
) -> None:
    async with make_session(unconfigured_settings) as session:
        await session.initialize()
        result = await session.call_tool("get_config_diff", {"device_name": ""})
    payload = _payload(result)
    assert payload == {"ok": False, "detail": "device_name must not be empty"}


async def test_config_diff_bad_time_format(
    make_session, unconfigured_settings: Settings
) -> None:
    async with make_session(unconfigured_settings) as session:
        await session.initialize()
        result = await session.call_tool(
            "get_config_diff",
            {
                "device_name": "R1",
                "start_time": "bad",
                "end_time": "also-bad",
            },
        )
    payload = _payload(result)
    assert payload["ok"] is False
    assert "must follow" in str(payload["detail"])


async def test_change_history_bad_page_num(
    make_session, unconfigured_settings: Settings
) -> None:
    async with make_session(unconfigured_settings) as session:
        await session.initialize()
        result = await session.call_tool("get_change_history", {"page_num": 0})
    payload = _payload(result)
    assert payload == {"ok": False, "detail": "page_num must be >= 1 (got 0)"}


async def test_change_history_bad_time_window(
    make_session, unconfigured_settings: Settings
) -> None:
    async with make_session(unconfigured_settings) as session:
        await session.initialize()
        result = await session.call_tool(
            "get_change_history", {"start_time": "2026-08-04 10:00:00"}
        )
    payload = _payload(result)
    assert payload == {
        "ok": False,
        "detail": "start_time and end_time must be provided together",
    }
