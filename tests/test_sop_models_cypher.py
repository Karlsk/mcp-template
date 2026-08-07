"""Tests for SOP models and Cypher constants/builders (spec-03 §3-§4,
schema realigned by spec-05 §3)."""

from __future__ import annotations

from app.graph import cypher
from app.graph.models import SOPCandidate, SOPNode, SOPTree


def test_cypher_budget_constants() -> None:
    """Cycle guards and budgets are frozen (spec-03 §4.5)."""
    assert cypher.MAX_SOP_DEPTH == 20
    assert cypher.MAX_SOP_NODES == 200
    assert cypher.MAX_SOP_CANDIDATES == 50


def test_cypher_schema_constants_match_the_real_graph() -> None:
    """spec-05 §3.1: labels/relationships/property names come from the real
    graph example code and are frozen here."""
    assert cypher.LABEL_EVENT == "Event"
    assert cypher.LABEL_STEP == "Step"
    assert cypher.LABEL_OUTPUT == "Output"
    assert cypher.REL_SEQUENCE == "Sequence"
    assert cypher.REL_BRANCH == "Branch"
    assert cypher.DB_PROPERTY == "database"
    assert cypher.PROP_ACTION == "Action"
    assert cypher.PROP_OBSERVATION == "Observation"
    assert cypher.PROP_FINAL_ANSWER == "FinalAnswer"
    assert cypher.PROP_CONDITION == "Condition"


def test_dual_case_exprs_cover_upper_and_lower_writes() -> None:
    """spec-05 §3.1: real data mixes Action/action etc.; the coalesce chains
    are the single place where both spellings are reconciled."""
    assert cypher.ACTION_EXPR == "coalesce(n.Action, n.action, '')"
    assert cypher.OBSERVATION_EXPR == "coalesce(n.Observation, n.observation, '')"
    assert cypher.FINAL_ANSWER_EXPR == (
        "coalesce(n.FinalAnswer, n.final_answer, n.reason, '')"
    )


def test_find_events_exact_matches_all_four_keys() -> None:
    stmt = cypher.FIND_EVENTS_EXACT
    assert ":Event" in stmt
    # Cross-db-safe db guard: parameter exists (as null) on the cross-db path.
    assert "$database IS NULL OR e.database = $database" in stmt
    # coalesce guards against Events that lack the optional property
    # (spec-05 §3.2 item 2).
    assert "toLower(coalesce(e.fault_type, '')) = $fault_type" in stmt
    assert "toLower(coalesce(e.intent, '')) = $intent" in stmt
    assert "toLower(e.name) = $needle" in stmt
    # aliases participate in exact matching.
    assert "coalesce(e.aliases, [])" in stmt
    # Every candidate echoes its own logical database and the name-based
    # locator; the internal id falls back to elementId (spec-05 §3.2).
    assert "e.database AS db" in stmt
    assert "e.name AS event_name" in stmt
    assert "coalesce(e.id, elementId(e)) AS event_id" in stmt
    assert "ORDER BY db, event_name" in stmt
    assert "LIMIT $limit" in stmt


def test_find_events_fuzzy_is_substring_fallback() -> None:
    stmt = cypher.FIND_EVENTS_FUZZY
    assert "$database IS NULL OR e.database = $database" in stmt
    assert "$needle IS NOT NULL" in stmt
    for field in ("e.name", "e.fault_type", "e.intent", "e.description"):
        assert f"toLower(coalesce({field}, '')) CONTAINS $needle" in stmt
    assert "ANY(a IN coalesce(e.aliases, [])" in stmt
    assert "e.database AS db" in stmt
    assert "e.name AS event_name" in stmt
    assert "coalesce(e.id, elementId(e)) AS event_id" in stmt


def test_sop_tree_nodes_inlines_validated_depth() -> None:
    """``max_depth`` is interpolated (Cypher cannot parameterize it), so the
    builder is the ONLY injection point and only ever receives a validated int."""
    stmt = cypher.sop_tree_nodes(5)
    assert "*1..5" in stmt
    # spec-05 §3.3: the traversal follows Sequence/Branch edges only.
    assert ":Sequence|Branch*1..5" in stmt
    # The single most error-prone lines: intermediate nodes AND relationships
    # must share the db (spec-05 §1.2 — both carry the tenancy property).
    assert "ALL(n IN nodes(path) WHERE n.database = $database)" in stmt
    assert "ALL(r IN relationships(path) WHERE r.database = $database)" in stmt
    # Locating goes through (db, event_name); toLower on both sides keeps the
    # match case-insensitive like the discovery stage (spec-05 §3.3).
    assert "e.database = $database AND toLower(e.name) = toLower($event_name)" in stmt
    # Single-node SOPs must still come back (no outgoing edges).
    assert "OPTIONAL MATCH" in stmt
    # labels feed ``kind``; properties preserve schema evolution.
    assert "labels(n) AS labels" in stmt
    assert "properties(n) AS props" in stmt
    # Dual-case value extraction happens via the frozen coalesce expressions.
    assert "coalesce(n.Action, n.action, '') AS action" in stmt
    assert "coalesce(n.Observation, n.observation, '') AS observation" in stmt
    assert (
        "coalesce(n.FinalAnswer, n.final_answer, n.reason, '') AS final_answer"
        in stmt
    )
    # Events lack a guaranteed id: fall back to elementId (spec-05 §3.3).
    assert "coalesce(n.id, elementId(n)) AS id" in stmt
    # Budget + one extra row to detect truncation.
    assert "LIMIT $node_limit" in stmt


def test_sop_tree_edges_locks_both_endpoints_and_relationships() -> None:
    stmt = cypher.SOP_TREE_EDGES
    # spec-05 §3.3: a/b/r all carry the tenancy property.
    assert (
        "a.database = $database AND b.database = $database"
        " AND r.database = $database" in stmt
    )
    assert (
        "coalesce(a.id, elementId(a)) IN $node_ids"
        " AND coalesce(b.id, elementId(b)) IN $node_ids" in stmt
    )
    assert "[r:Sequence|Branch]" in stmt
    # Edge type + condition echo: Sequence edges come back with null condition.
    assert "type(r) AS rel_type" in stmt
    assert "r.Condition AS condition" in stmt


def test_resolve_event_by_name_is_cross_db_safe() -> None:
    """spec-05 §3.2: locating always goes through the Event name."""
    stmt = cypher.RESOLVE_EVENT_BY_NAME
    assert ":Event" in stmt
    assert "$database IS NULL OR e.database = $database" in stmt
    assert "toLower(e.name) = $event_name" in stmt
    assert "e.database AS db" in stmt
    assert "e.name AS event_name" in stmt
    assert "coalesce(e.id, elementId(e)) AS event_id" in stmt


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
    candidate = SOPCandidate.model_validate(
        {"db": "lib_a", "event_name": "link-down", "event_id": "E1"}
    )
    assert candidate.event_name == "link-down"
    # The internal id is echoed for cross-session references, kept via extra.
    assert candidate.model_extra is not None
    assert candidate.model_extra["event_id"] == "E1"
    assert candidate.fault_type == ""
    assert candidate.intent == ""

    event = SOPNode(id="E1", kind="event", name="link-down")
    tree = SOPTree(db="lib_a", event=event)
    assert tree.nodes == []
    assert tree.edges == []
    assert tree.truncated is False
