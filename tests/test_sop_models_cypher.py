"""Tests for SOP models and Cypher constants/builders (spec-03 §3-§4)."""

from __future__ import annotations

from app.graph import cypher
from app.graph.models import SOPCandidate, SOPNode, SOPTree


def test_cypher_budget_constants() -> None:
    """Cycle guards and budgets are frozen (spec-03 §4.5)."""
    assert cypher.MAX_SOP_DEPTH == 20
    assert cypher.MAX_SOP_NODES == 200
    assert cypher.MAX_SOP_CANDIDATES == 50


def test_find_events_exact_matches_all_four_keys() -> None:
    stmt = cypher.FIND_EVENTS_EXACT
    assert ":Event" in stmt
    # Cross-db-safe db guard: parameter exists (as null) on the cross-db path.
    assert "$_db IS NULL OR e._db = $_db" in stmt
    assert "toLower(e.fault_type) = $fault_type" in stmt
    assert "toLower(e.intent) = $intent" in stmt
    assert "toLower(e.name) = $needle" in stmt
    # aliases participate in exact matching.
    assert "coalesce(e.aliases, [])" in stmt
    # Every candidate echoes its own logical database.
    assert "e._db AS db" in stmt
    assert "LIMIT $limit" in stmt


def test_find_events_fuzzy_is_substring_fallback() -> None:
    stmt = cypher.FIND_EVENTS_FUZZY
    assert "$_db IS NULL OR e._db = $_db" in stmt
    assert "$needle IS NOT NULL" in stmt
    for field in ("e.name", "e.fault_type", "e.intent", "e.description"):
        assert f"toLower(coalesce({field}, '')) CONTAINS $needle" in stmt
    assert "ANY(a IN coalesce(e.aliases, [])" in stmt
    assert "e._db AS db" in stmt


def test_sop_tree_nodes_inlines_validated_depth() -> None:
    """``max_depth`` is interpolated (Cypher cannot parameterize it), so the
    builder is the ONLY injection point and only ever receives a validated int."""
    stmt = cypher.sop_tree_nodes(5)
    assert "*1..5" in stmt
    # The single most error-prone line: intermediate nodes must share the db.
    assert "ALL(n IN nodes(path) WHERE n._db = $_db)" in stmt
    assert "e._db = $_db AND e.id = $event_id" in stmt
    # Single-node SOPs must still come back (no outgoing edges).
    assert "OPTIONAL MATCH" in stmt
    # labels feed ``kind``; properties preserve schema evolution.
    assert "labels(n) AS labels" in stmt
    assert "properties(n) AS props" in stmt
    # Budget + one extra row to detect truncation.
    assert "LIMIT $node_limit" in stmt


def test_sop_tree_edges_locks_both_endpoints() -> None:
    stmt = cypher.SOP_TREE_EDGES
    assert "a._db = $_db AND b._db = $_db" in stmt
    assert "a.id IN $node_ids AND b.id IN $node_ids" in stmt
    # Missing condition property comes back as null -> ``condition is None``.
    assert "r.condition AS condition" in stmt


def test_resolve_event_by_id_is_cross_db_safe() -> None:
    stmt = cypher.RESOLVE_EVENT_BY_ID
    assert ":Event" in stmt
    assert "$_db IS NULL OR e._db = $_db" in stmt
    assert "e.id = $event_id" in stmt
    assert "e._db AS db" in stmt


def test_sop_node_defaults_and_extra_allow() -> None:
    node = SOPNode()
    assert node.id == ""
    assert node.kind == ""
    assert node.name == ""
    assert node.action == ""
    assert node.observation == ""
    assert node.answer == ""
    flexible = SOPNode.model_validate({"id": "S1", "kind": "step", "vendor_extra": 1})
    assert flexible.vendor_extra == 1  # type: ignore[attr-defined]


def test_sop_candidate_and_tree_shapes() -> None:
    candidate = SOPCandidate.model_validate({"db": "lib_a", "event_id": "E1"})
    assert candidate.name == ""
    assert candidate.fault_type == ""
    assert candidate.intent == ""

    event = SOPNode(id="E1", kind="event", name="link-down")
    tree = SOPTree(db="lib_a", event=event)
    assert tree.nodes == []
    assert tree.edges == []
    assert tree.truncated is False
