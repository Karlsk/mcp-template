"""Tool-layer tests for search_sop over in-memory MCP sessions (spec-03 §7)."""

from __future__ import annotations

import json
from typing import Any

from mcp.types import TextContent
from neo4j.exceptions import ServiceUnavailable
from pydantic import SecretStr

from app.graph import GraphClient
from app.graph.cypher import FIND_EVENTS_EXACT, FIND_EVENTS_FUZZY, SOP_TREE_EDGES
from app.settings import Settings
from app.tools.validation import GRAPH_SKELETON_DETAIL
from tests.conftest import (
    FakeNeo4jDriver,
    make_graph_settings,
    make_sdn_settings,
)

CANDIDATE_ROW = {
    "db": "lib_a",
    "event_name": "Link Down",
    "event_id": "E1",
    "fault_type": "link down",
    "intent": "",
}
TREE_NODES = [
    {
        "id": "E1", "labels": ["Event"], "name": "Link Down",
        "action": "", "observation": "", "final_answer": "",
        "props": {"id": "E1", "name": "Link Down", "database": "lib_a"},
    },
    {
        "id": "S1", "labels": ["Step"], "name": "Check interface",
        "action": "verify_interface_state", "observation": "oper_state",
        "final_answer": "", "props": {"id": "S1", "name": "Check interface", "database": "lib_a"},
    },
    {
        "id": "O1", "labels": ["Output"], "name": "Close",
        "action": "", "observation": "", "final_answer": "Link is fine.",
        "props": {"id": "O1", "name": "Close", "database": "lib_a"},
    },
]
TREE_EDGES = [
    {"source": "E1", "target": "S1", "rel_type": "Sequence", "condition": None},
    {"source": "S1", "target": "O1", "rel_type": "Branch", "condition": "oper_state=down"},
]


def _payload(result: Any) -> dict[str, Any]:
    if getattr(result, "structuredContent", None):
        return dict(result.structuredContent)
    block = result.content[0]
    assert isinstance(block, TextContent), f"unexpected content: {block!r}"
    return json.loads(block.text)


def make_settings(neo4j_uri: str = "bolt://fake:7687") -> Settings:
    return Settings(
        sdn=make_sdn_settings("https://sdn.example"),
        sdn_controller_token=SecretStr("tok"),
        neo4j=make_graph_settings(neo4j_uri),
    )


def sop_handler(candidates: list[dict[str, Any]]) -> Any:
    """Serve discovery, tree-nodes and tree-edges queries from one fake."""

    def handler(cypher: str, _params: dict[str, Any]) -> list[dict[str, Any]]:
        if cypher in (FIND_EVENTS_EXACT, FIND_EVENTS_FUZZY):
            return candidates
        if cypher == SOP_TREE_EDGES:
            return TREE_EDGES
        return TREE_NODES

    return handler


def graph_factory(driver: FakeNeo4jDriver) -> Any:
    settings = make_settings()
    return lambda: GraphClient(settings, driver=driver)


# ---------------------------------------------------------------------------
# §7.1 registration surface
# ---------------------------------------------------------------------------


async def test_description_keeps_the_spec04_handoff_hint(
    make_session,
) -> None:
    async with make_session(make_settings("")) as session:
        await session.initialize()
        tools = (await session.list_tools()).tools
    tool = next(t for t in tools if t.name == "search_sop")
    assert tool.description is not None
    assert "search_command_template" in tool.description


# ---------------------------------------------------------------------------
# §7.2-3 input validation (safe detail, never raises)
# ---------------------------------------------------------------------------


async def test_all_search_keys_empty_is_rejected(make_session) -> None:
    async with make_session(make_settings()) as session:
        await session.initialize()
        result = await session.call_tool("search_sop", {})
    payload = _payload(result)
    assert payload["ok"] is False
    assert "fault_type / intent / keyword / event_id" in str(payload["detail"])


async def test_db_alone_is_rejected_because_it_is_a_scope_not_a_key(
    make_session,
) -> None:
    async with make_session(make_settings()) as session:
        await session.initialize()
        result = await session.call_tool("search_sop", {"db": "lib_a"})
    payload = _payload(result)
    assert payload["ok"] is False
    assert "fault_type / intent / keyword / event_id" in str(payload["detail"])


async def test_limit_and_depth_bounds_are_validated_before_any_query(
    make_session,
) -> None:
    driver = FakeNeo4jDriver()
    async with make_session(
        make_settings(), graph_client_factory=graph_factory(driver)
    ) as session:
        await session.initialize()
        cases = [
            ({"fault_type": "x", "limit": 0}, "limit"),
            ({"fault_type": "x", "limit": 51}, "limit"),
            ({"fault_type": "x", "max_depth": 0}, "max_depth"),
            ({"fault_type": "x", "max_depth": 21}, "max_depth"),
        ]
        for args, name in cases:
            result = await session.call_tool("search_sop", args)
            payload = _payload(result)
            assert payload["ok"] is False
            assert str(payload["detail"]).startswith(f"{name} must be in")
    # §7.10 (tool half): max_depth=999 dies in validation — no cypher at all.
    assert driver.calls == []


# ---------------------------------------------------------------------------
# §7.4 skeleton mode
# ---------------------------------------------------------------------------


async def test_skeleton_mode_when_graph_not_configured(make_session) -> None:
    async with make_session(make_settings("")) as session:
        await session.initialize()
        result = await session.call_tool("search_sop", {"fault_type": "link down"})
    payload = _payload(result)
    assert payload["ok"] is False
    assert payload["configured"] is False
    assert payload["detail"] == GRAPH_SKELETON_DETAIL


# ---------------------------------------------------------------------------
# §7.5-6 error paths stay sanitized
# ---------------------------------------------------------------------------


async def test_graph_error_returns_safe_message_without_uri_leak(
    make_session,
) -> None:
    secret = "bolt://secret-host:7687"
    driver = FakeNeo4jDriver(run_exc=ServiceUnavailable(f"{secret} is down"))
    async with make_session(
        make_settings(), graph_client_factory=graph_factory(driver)
    ) as session:
        await session.initialize()
        result = await session.call_tool("search_sop", {"fault_type": "link down"})
    payload = _payload(result)
    assert payload["ok"] is False
    assert payload["configured"] is True
    assert payload["detail"] == "SOP graph is unreachable."
    assert secret not in json.dumps(payload)


class _BrokenGraph:
    """Non-GraphError failure path: any exception becomes the generic envelope."""

    configured = True

    async def probe(self) -> bool:
        return True

    async def aclose(self) -> None:
        return None

    async def find_sop_events(self, **_kwargs: Any) -> Any:
        raise RuntimeError("boom")


async def test_unexpected_error_returns_generic_envelope(make_session) -> None:
    async with make_session(
        make_settings(),
        graph_client_factory=lambda: _BrokenGraph(),  # type: ignore[return-value]
    ) as session:
        await session.initialize()
        result = await session.call_tool("search_sop", {"fault_type": "link down"})
    payload = _payload(result)
    assert payload["ok"] is False
    assert payload["detail"] == "Unexpected server error."
    assert "boom" not in json.dumps(payload)


# ---------------------------------------------------------------------------
# §7.7 the three output modes
# ---------------------------------------------------------------------------


async def test_single_hit_returns_the_tree_mode(make_session) -> None:
    driver = FakeNeo4jDriver(records_handler=sop_handler([CANDIDATE_ROW]))
    async with make_session(
        make_settings(), graph_client_factory=graph_factory(driver)
    ) as session:
        await session.initialize()
        result = await session.call_tool("search_sop", {"fault_type": "link down"})
    payload = _payload(result)
    assert payload["ok"] is True
    assert payload["mode"] == "tree"
    assert payload["match"] == "exact"
    assert payload["db"] == "lib_a"
    # spec-05 §5: the event root becomes the name-based locator payload.
    assert payload["event"] == {
        "event_name": "Link Down", "event_id": "E1", "fault_type": "", "intent": "",
    }
    assert len(payload["nodes"]) == 3
    assert len(payload["edges"]) == 2
    assert payload["truncated"] is False
    assert "search_command_template" in payload["next_step_hint"]
    # spec-05 §5 envelope: type/label are derived, empty strings omitted.
    by_id = {n["id"]: n for n in payload["nodes"]}
    assert by_id["S1"] == {
        "id": "S1", "name": "Check interface", "type": "function_call",
        "label": "Step", "action": "verify_interface_state",
        "observation": "oper_state",
    }
    assert by_id["O1"] == {
        "id": "O1", "name": "Close", "type": "final_answer",
        "label": "Output", "reason": "Link is fine.",
    }
    # Deliberate redundancy: the event is also inside nodes (function_call,
    # no action -> no optional keys).
    assert by_id["E1"]["type"] == "function_call"
    assert by_id["E1"]["label"] == "Event"
    assert "action" not in by_id["E1"]
    # spec-05 §5: edges echo rel_type; Sequence edges keep condition null.
    by_pair = {(e["source"], e["target"]): e for e in payload["edges"]}
    assert by_pair[("E1", "S1")]["rel_type"] == "Sequence"
    assert by_pair[("E1", "S1")]["condition"] is None
    assert by_pair[("S1", "O1")]["rel_type"] == "Branch"
    assert by_pair[("S1", "O1")]["condition"] == "oper_state=down"


async def test_multiple_hits_return_candidates_mode(make_session) -> None:
    second = dict(CANDIDATE_ROW, db="lib_b", event_id="E9")
    driver = FakeNeo4jDriver(records_handler=sop_handler([CANDIDATE_ROW, second]))
    async with make_session(
        make_settings(), graph_client_factory=graph_factory(driver)
    ) as session:
        await session.initialize()
        result = await session.call_tool("search_sop", {"keyword": "link"})
    payload = _payload(result)
    assert payload["ok"] is True
    assert payload["mode"] == "candidates"
    assert len(payload["candidates"]) == 2
    for candidate in payload["candidates"]:
        assert candidate["db"]
        # spec-05 §5: locating goes through the name; event_id is only echoed.
        assert candidate["event_name"] == "Link Down"
    assert "db and event_name" in payload["next_step_hint"]


async def test_zero_hits_is_ok_true_with_empty_mode(make_session) -> None:
    driver = FakeNeo4jDriver(records_handler=sop_handler([]))
    async with make_session(
        make_settings(), graph_client_factory=graph_factory(driver)
    ) as session:
        await session.initialize()
        result = await session.call_tool("search_sop", {"fault_type": "nothing"})
    payload = _payload(result)
    # Empty result is a successful query: ok stays True (spec-03 §6.3).
    assert payload["ok"] is True
    assert payload["mode"] == "empty"
    assert payload["match"] == "none"
    assert payload["candidates"] == []


async def test_db_plus_event_id_expands_directly(make_session) -> None:
    driver = FakeNeo4jDriver(records_handler=sop_handler([]))
    async with make_session(
        make_settings(), graph_client_factory=graph_factory(driver)
    ) as session:
        await session.initialize()
        result = await session.call_tool(
            "search_sop", {"db": "lib_a", "event_id": "E1"}
        )
    payload = _payload(result)
    assert payload["ok"] is True
    assert payload["mode"] == "tree"
    # No discovery query: (db, event_name) locates the tree directly.
    assert all(c["cypher"] != FIND_EVENTS_EXACT for c in driver.calls)


async def test_event_id_without_db_reverse_looks_up(make_session) -> None:
    """Ambiguous names return candidates instead of guessing (spec-05 §3.2)."""
    from app.graph.cypher import RESOLVE_EVENT_BY_NAME

    def handler(cypher: str, _params: dict[str, Any]) -> list[dict[str, Any]]:
        if cypher == RESOLVE_EVENT_BY_NAME:
            return [CANDIDATE_ROW, dict(CANDIDATE_ROW, db="lib_b")]
        if cypher == SOP_TREE_EDGES:
            return TREE_EDGES
        return TREE_NODES

    driver = FakeNeo4jDriver(records_handler=handler)
    async with make_session(
        make_settings(), graph_client_factory=graph_factory(driver)
    ) as session:
        await session.initialize()
        result = await session.call_tool("search_sop", {"event_id": "E1"})
    payload = _payload(result)
    assert payload["ok"] is True
    assert payload["mode"] == "candidates"
    assert len(payload["candidates"]) == 2
