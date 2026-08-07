"""Client-layer tests for the SOP business methods (spec-03 §5, §7 items 1-10)."""

from __future__ import annotations

from typing import Any

import pytest

from app.graph import GraphClient, GraphQueryError
from app.graph.cypher import (
    FIND_EVENTS_EXACT,
    FIND_EVENTS_FUZZY,
    MAX_SOP_NODES,
    RESOLVE_EVENT_BY_ID,
    SOP_TREE_EDGES,
    sop_tree_nodes,
)
from app.settings import Settings
from tests.conftest import FakeNeo4jDriver, make_graph_settings, make_sdn_settings

CANDIDATE_ROW = {
    "db": "lib_a",
    "event_id": "E1",
    "name": "Link Down",
    "fault_type": "link down",
    "intent": "",
}


def make_settings(uri: str = "bolt://graph.example:7687") -> Settings:
    return Settings(sdn=make_sdn_settings(""), neo4j=make_graph_settings(uri))


def node_row(
    node_id: str, label: str, *, name: str = "", db: str = "lib_a", **extra: Any
) -> dict[str, Any]:
    """One row shaped like the sop_tree_nodes RETURN clause."""
    return {
        "id": node_id,
        "labels": [label],
        "name": name,
        "action": extra.pop("action", ""),
        "observation": extra.pop("observation", ""),
        "answer": extra.pop("answer", ""),
        "props": {"id": node_id, "name": name, "database": db, **extra},
    }


def tree_handler(
    nodes: list[dict[str, Any]], edges: list[dict[str, Any]]
) -> Any:
    def handler(cypher: str, _params: dict[str, Any]) -> list[dict[str, Any]]:
        return edges if cypher == SOP_TREE_EDGES else nodes

    return handler


class RunReadSpy:
    """Records (db_tag, allow_cross_db) exactly as passed to run_read."""

    def __init__(self, graph: GraphClient) -> None:
        self.calls: list[dict[str, Any]] = []
        self._original = graph._neo4j.run_read

    async def __call__(
        self,
        cypher: str,
        params: dict[str, Any] | None = None,
        *,
        db_tag: str | None,
        allow_cross_db: bool = False,
        query_name: str = "query",
    ) -> list[dict[str, Any]]:
        self.calls.append({"db_tag": db_tag, "allow_cross_db": allow_cross_db})
        return await self._original(
            cypher, params, db_tag=db_tag, allow_cross_db=allow_cross_db,
            query_name=query_name,
        )


# ---------------------------------------------------------------------------
# Discovery stage: find_sop_events
# ---------------------------------------------------------------------------


async def test_exact_hit_single_candidate_no_fuzzy_query() -> None:
    """Spec §7.1: one exact hit -> one query only, fuzzy never runs."""
    driver = FakeNeo4jDriver(records=[CANDIDATE_ROW])
    graph = GraphClient(make_settings(), driver=driver)

    candidates, mode = await graph.find_sop_events(fault_type="  Link DOWN  ")

    assert mode == "exact"
    assert len(candidates) == 1
    assert candidates[0].db == "lib_a"
    assert candidates[0].event_id == "E1"
    assert len(driver.calls) == 1
    assert driver.calls[0]["cypher"] == FIND_EVENTS_EXACT
    # Both sides normalize: client strips + lowers before the toLower() compare.
    assert driver.calls[0]["params"]["fault_type"] == "link down"


async def test_exact_miss_degrades_to_fuzzy() -> None:
    """Spec §7.2: zero exact hits -> second query is FIND_EVENTS_FUZZY."""

    def handler(cypher: str, _params: dict[str, Any]) -> list[dict[str, Any]]:
        return [CANDIDATE_ROW] if cypher == FIND_EVENTS_FUZZY else []

    driver = FakeNeo4jDriver(records_handler=handler)
    graph = GraphClient(make_settings(), driver=driver)

    candidates, mode = await graph.find_sop_events(keyword="link")

    assert mode == "fuzzy"
    assert len(candidates) == 1
    assert len(driver.calls) == 2
    assert driver.calls[0]["cypher"] == FIND_EVENTS_EXACT
    assert driver.calls[1]["cypher"] == FIND_EVENTS_FUZZY


async def test_both_stages_miss_returns_none_mode_without_third_query() -> None:
    """Spec §7.3: empty list + match 'none', and no third query is issued."""
    driver = FakeNeo4jDriver(records=[])
    graph = GraphClient(make_settings(), driver=driver)

    candidates, mode = await graph.find_sop_events(fault_type="nope")

    assert candidates == []
    assert mode == "none"
    assert len(driver.calls) == 2


async def test_needle_prefers_keyword_and_is_normalized() -> None:
    driver = FakeNeo4jDriver(records=[])
    graph = GraphClient(make_settings(), driver=driver)

    await graph.find_sop_events(fault_type="F", intent="I", keyword="  Kw ")

    params = driver.calls[0]["params"]
    assert params["needle"] == "kw"
    assert params["fault_type"] == "f"
    assert params["intent"] == "i"


# ---------------------------------------------------------------------------
# Spec §7.8: db isolation — the two-stage db semantics
# ---------------------------------------------------------------------------


async def test_discovery_is_cross_db_with_explicit_null_db_param() -> None:
    """Spec §7.8a: db_tag=None + allow_cross_db=True + params['database'] is
    None (present, not absent — a real Neo4j would raise ParameterMissing)."""
    driver = FakeNeo4jDriver(records=[CANDIDATE_ROW])
    graph = GraphClient(make_settings(), driver=driver)
    spy = RunReadSpy(graph)
    graph._neo4j.run_read = spy  # type: ignore[method-assign]

    await graph.find_sop_events(fault_type="x")

    assert spy.calls[0]["db_tag"] is None
    assert spy.calls[0]["allow_cross_db"] is True
    assert "database" in driver.calls[0]["params"]
    assert driver.calls[0]["params"]["database"] is None


async def test_discovery_narrowed_to_one_db_is_not_cross_db() -> None:
    """Spec §7.8b: db='lib_a' -> db_tag='lib_a', allow_cross_db False."""
    driver = FakeNeo4jDriver(records=[CANDIDATE_ROW])
    graph = GraphClient(make_settings(), driver=driver)
    spy = RunReadSpy(graph)
    graph._neo4j.run_read = spy  # type: ignore[method-assign]

    await graph.find_sop_events(fault_type="x", db="lib_a")

    assert spy.calls[0]["db_tag"] == "lib_a"
    assert spy.calls[0]["allow_cross_db"] is False
    # run_read overwrites the preset None with the db_tag.
    assert driver.calls[0]["params"]["database"] == "lib_a"


async def test_resolve_sop_event_is_cross_db_reverse_lookup() -> None:
    driver = FakeNeo4jDriver(records=[CANDIDATE_ROW])
    graph = GraphClient(make_settings(), driver=driver)
    spy = RunReadSpy(graph)
    graph._neo4j.run_read = spy  # type: ignore[method-assign]

    found = await graph.resolve_sop_event("E1")

    assert len(found) == 1
    assert found[0].event_id == "E1"
    assert driver.calls[0]["cypher"] == RESOLVE_EVENT_BY_ID
    assert driver.calls[0]["params"] == {"database": None, "event_id": "E1"}
    assert spy.calls[0]["db_tag"] is None
    assert spy.calls[0]["allow_cross_db"] is True


# ---------------------------------------------------------------------------
# Tree expansion: get_sop_tree
# ---------------------------------------------------------------------------

BRANCH_NODES = [
    node_row("E1", "Event", name="Link Down"),
    node_row("S1", "Step", name="Check interface", action="verify_interface_state",
             observation="oper_state"),
    node_row("S2", "Step", name="Check optics", action="verify_optics",
             observation="rx_power"),
    node_row("S3", "Step", name="Check neighbor", action="verify_neighbor"),
    node_row("O1", "Output", name="Close", answer="Link is fine."),
    node_row("O2", "Output", name="Escalate", answer="Open a ticket."),
]
BRANCH_EDGES = [
    {"source": "E1", "target": "S1", "condition": None},
    {"source": "S1", "target": "S2", "condition": "oper_state=up"},
    {"source": "S1", "target": "O1", "condition": "oper_state=down"},
    {"source": "S2", "target": "S3", "condition": None},
    {"source": "S3", "target": "O2", "condition": None},
]


async def test_tree_shapes_nodes_kinds_and_edge_conditions() -> None:
    """Spec §7.4: Event + 3 Steps + 2 Outputs, branch vs plain edges."""
    driver = FakeNeo4jDriver(records_handler=tree_handler(BRANCH_NODES, BRANCH_EDGES))
    graph = GraphClient(make_settings(), driver=driver)

    tree = await graph.get_sop_tree(db="lib_a", event_id="E1")

    assert tree is not None
    assert tree.db == "lib_a"
    assert tree.event.id == "E1"
    assert tree.event.kind == "event"
    assert len(tree.nodes) == 6
    kinds = {n.id: n.kind for n in tree.nodes}
    assert kinds == {"E1": "event", "S1": "step", "S2": "step", "S3": "step",
                     "O1": "output", "O2": "output"}
    s1 = next(n for n in tree.nodes if n.id == "S1")
    assert s1.action == "verify_interface_state"
    assert s1.observation == "oper_state"
    # props are merged via extra="allow" (schema evolution preserved).
    assert s1.model_extra is not None
    assert s1.model_extra["database"] == "lib_a"
    by_pair = {(e.source, e.target): e.condition for e in tree.edges}
    assert by_pair[("S1", "S2")] == "oper_state=up"   # branch edge
    assert by_pair[("E1", "S1")] is None              # plain edge, not ""
    assert tree.truncated is False


async def test_tree_expansion_locks_the_logical_db() -> None:
    """Spec §7.8c: both queries (nodes + edges) carry db_tag='lib_a'."""
    driver = FakeNeo4jDriver(records_handler=tree_handler(BRANCH_NODES, BRANCH_EDGES))
    graph = GraphClient(make_settings(), driver=driver)
    spy = RunReadSpy(graph)
    graph._neo4j.run_read = spy  # type: ignore[method-assign]

    await graph.get_sop_tree(db="lib_a", event_id="E1")

    assert [c["db_tag"] for c in spy.calls] == ["lib_a", "lib_a"]
    assert all(c["allow_cross_db"] is False for c in spy.calls)
    assert [c["params"]["database"] for c in driver.calls] == ["lib_a", "lib_a"]


async def test_tree_truncation_when_nodes_exceed_budget() -> None:
    """Spec §7.5: > MAX_SOP_NODES rows -> truncated, nodes capped."""
    rows = [node_row("E1", "Event", name="root")]
    rows += [node_row(f"S{i}", "Step") for i in range(MAX_SOP_NODES)]
    driver = FakeNeo4jDriver(records_handler=tree_handler(rows, []))
    graph = GraphClient(make_settings(), driver=driver)

    tree = await graph.get_sop_tree(db="lib_a", event_id="E1")

    assert tree is not None
    assert tree.truncated is True
    assert len(tree.nodes) == MAX_SOP_NODES


async def test_cyclic_graph_returns_without_hanging() -> None:
    """Spec §7.6: cycles in dirty data must not hang Python-side assembly."""
    nodes = [
        node_row("E1", "Event"),
        node_row("S1", "Step"),
        node_row("S2", "Step"),
    ]
    edges = [
        {"source": "E1", "target": "S1", "condition": None},
        {"source": "S1", "target": "S2", "condition": None},
        {"source": "S2", "target": "S1", "condition": None},  # cycle
    ]
    driver = FakeNeo4jDriver(records_handler=tree_handler(nodes, edges))
    graph = GraphClient(make_settings(), driver=driver)

    tree = await graph.get_sop_tree(db="lib_a", event_id="E1")

    assert tree is not None
    assert len(tree.nodes) == 3
    assert len(tree.edges) == 3


async def test_tree_without_event_node_raises_safe_error() -> None:
    """Spec §7.7: dirty data without an Event -> sanitized GraphQueryError."""
    nodes = [node_row("S1", "Step")]
    driver = FakeNeo4jDriver(records_handler=tree_handler(nodes, []))
    graph = GraphClient(make_settings(), driver=driver)

    with pytest.raises(GraphQueryError) as excinfo:
        await graph.get_sop_tree(db="lib_a", event_id="E1")

    assert str(excinfo.value) == "SOP graph response was malformed."
    assert "lib_a" in excinfo.value.detail
    assert "E1" in excinfo.value.detail


async def test_tree_not_found_returns_none() -> None:
    driver = FakeNeo4jDriver(records=[])
    graph = GraphClient(make_settings(), driver=driver)

    assert await graph.get_sop_tree(db="lib_a", event_id="missing") is None


async def test_cross_db_same_id_does_not_merge_trees() -> None:
    """Spec §7.9: lib_a and lib_b both own E1 — trees stay disjoint."""

    def handler(cypher: str, params: dict[str, Any]) -> list[dict[str, Any]]:
        db = params["database"]
        if cypher == SOP_TREE_EDGES:
            return [{"source": "E1", "target": f"{db}-S1", "condition": None}]
        return [
            node_row("E1", "Event", db=db),
            node_row(f"{db}-S1", "Step", db=db),
        ]

    driver = FakeNeo4jDriver(records_handler=handler)
    graph = GraphClient(make_settings(), driver=driver)

    tree_a = await graph.get_sop_tree(db="lib_a", event_id="E1")
    tree_b = await graph.get_sop_tree(db="lib_b", event_id="E1")

    assert tree_a is not None and tree_b is not None
    ids_a = {n.id for n in tree_a.nodes}
    ids_b = {n.id for n in tree_b.nodes}
    # Only the (db-scoped) root id overlaps in text; members differ.
    assert "lib_a-S1" in ids_a and "lib_a-S1" not in ids_b
    assert "lib_b-S1" in ids_b and "lib_b-S1" not in ids_a
    assert tree_a.event.model_extra is not None
    assert tree_a.event.model_extra["database"] == "lib_a"
    assert tree_b.event.model_extra is not None
    assert tree_b.event.model_extra["database"] == "lib_b"


async def test_max_depth_is_inlined_into_the_nodes_query() -> None:
    """Spec §7.10 (client half): max_depth=5 -> cypher text carries *1..5."""
    driver = FakeNeo4jDriver(records_handler=tree_handler(BRANCH_NODES, BRANCH_EDGES))
    graph = GraphClient(make_settings(), driver=driver)

    await graph.get_sop_tree(db="lib_a", event_id="E1", max_depth=5)

    assert driver.calls[0]["cypher"] == sop_tree_nodes(5)
    assert "*1..5" in driver.calls[0]["cypher"]
