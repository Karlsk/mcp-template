"""SOP graph labels, relationship types, and Cypher statement constants.

Aligning with the real graph only touches this file — never the client logic.
Statement bodies follow spec-03 §4 as realigned by spec-05 §3 (real graph
schema: ``Sequence``/``Branch`` relationships, capitalized ``Action`` /
``Observation`` / ``FinalAnswer`` properties, ``database`` tenancy property).
"""

from __future__ import annotations

from typing import Final

LABEL_EVENT: Final = "Event"
LABEL_STEP: Final = "Step"
LABEL_OUTPUT: Final = "Output"
REL_NEXT: Final = "NEXT"  # legacy spec-03 relationship; tree queries only
REL_SEQUENCE: Final = "Sequence"
REL_BRANCH: Final = "Branch"
DB_PROPERTY: Final = "database"    # real property name; nodes AND relationships

PROP_ACTION: Final = "Action"
PROP_OBSERVATION: Final = "Observation"
PROP_FINAL_ANSWER: Final = "FinalAnswer"
PROP_CONDITION: Final = "Condition"

# Dual-case-compatible value expressions (Cypher fragments inlined into the
# query templates; the node variable is hard-coded to ``n``). Real data mixes
# upper- and lower-case property writes, and the coalesce chains here are the
# ONE place both spellings are reconciled (spec-05 §3.1).
ACTION_EXPR: Final = "coalesce(n.Action, n.action, '')"
OBSERVATION_EXPR: Final = "coalesce(n.Observation, n.observation, '')"
FINAL_ANSWER_EXPR: Final = "coalesce(n.FinalAnswer, n.final_answer, n.reason, '')"

MAX_SOP_DEPTH: Final = 20      # variable-length traversal cap (cycle guard)
MAX_SOP_NODES: Final = 200     # node budget per tree
MAX_SOP_CANDIDATES: Final = 50 # upper bound for the `limit` tool parameter

# -- Discovery stage (spec-03 §4.1) ------------------------------------------
# ``($database IS NULL OR ...)``: callers MUST pass an explicit
# ``{"database": None}`` on the cross-db path — Cypher referencing a parameter
# that was never sent is rejected by Neo4j (ParameterMissing).
FIND_EVENTS_EXACT = f"""
MATCH (e:{LABEL_EVENT})
WHERE ($database IS NULL OR e.{DB_PROPERTY} = $database)
  AND (
    ($fault_type IS NOT NULL AND toLower(coalesce(e.fault_type, '')) = $fault_type)
    OR ($intent IS NOT NULL AND toLower(coalesce(e.intent, '')) = $intent)
    OR ($needle IS NOT NULL AND toLower(e.name) = $needle)
    OR ($needle IS NOT NULL AND $needle IN [a IN coalesce(e.aliases, []) | toLower(a)])
  )
RETURN e.{DB_PROPERTY} AS db, e.name AS event_name,
       coalesce(e.id, elementId(e)) AS event_id,
       coalesce(e.fault_type, '') AS fault_type, coalesce(e.intent, '') AS intent
ORDER BY db, event_name
LIMIT $limit
"""

# Fuzzy degradation (spec-03 §4.2): only issued when the exact stage found
# nothing — the two semantics ("is it" vs "might be it") stay separate.
FIND_EVENTS_FUZZY = f"""
MATCH (e:{LABEL_EVENT})
WHERE ($database IS NULL OR e.{DB_PROPERTY} = $database)
  AND $needle IS NOT NULL
  AND (
    toLower(coalesce(e.name, '')) CONTAINS $needle
    OR toLower(coalesce(e.fault_type, '')) CONTAINS $needle
    OR toLower(coalesce(e.intent, '')) CONTAINS $needle
    OR toLower(coalesce(e.description, '')) CONTAINS $needle
    OR ANY(a IN coalesce(e.aliases, []) WHERE toLower(a) CONTAINS $needle)
  )
RETURN e.{DB_PROPERTY} AS db, e.name AS event_name,
       coalesce(e.id, elementId(e)) AS event_id,
       coalesce(e.fault_type, '') AS fault_type, coalesce(e.intent, '') AS intent
ORDER BY db, event_name
LIMIT $limit
"""

# Reverse lookup by name across logical databases (spec-05 §3.2). Locating
# always goes through the Event name — the real graph does not guarantee an
# ``id`` property on Events; ``event_id`` is only echoed for references.
RESOLVE_EVENT_BY_NAME = f"""
MATCH (e:{LABEL_EVENT})
WHERE ($database IS NULL OR e.{DB_PROPERTY} = $database)
  AND toLower(e.name) = $event_name
RETURN e.{DB_PROPERTY} AS db, e.name AS event_name,
       coalesce(e.id, elementId(e)) AS event_id,
       coalesce(e.fault_type, '') AS fault_type, coalesce(e.intent, '') AS intent
ORDER BY db, event_name
"""


def sop_tree_nodes(max_depth: int) -> str:
    """Build the reachable-nodes query. ``max_depth`` MUST be a validated int:
    Cypher does not allow a parameterized variable-length upper bound, so it is
    interpolated into the statement text.

    Injection safety is guaranteed three ways (spec-03 §4.4):

    1. the tool layer rejects out-of-bound values via
       ``positive_bound_detail("max_depth", ...)`` before any client call;
    2. the client coerces with ``int(max_depth)`` and clamps to
       ``MAX_SOP_DEPTH`` as a second line of defense;
    3. this function is the ONLY interpolation point in the codebase and only
       ever receives that one integer — no user string enters Cypher text.
    """
    return f"""
MATCH (e:{LABEL_EVENT})
WHERE e.{DB_PROPERTY} = $database AND e.id = $event_id
OPTIONAL MATCH path = (e)-[:{REL_NEXT}*1..{max_depth}]->(m)
WHERE ALL(n IN nodes(path) WHERE n.{DB_PROPERTY} = $database)
WITH e, collect(DISTINCT m) AS reached
UNWIND ([e] + reached) AS n
WITH DISTINCT n WHERE n IS NOT NULL
RETURN n.id AS id, labels(n) AS labels, n.name AS name,
       coalesce(n.action, '') AS action,
       coalesce(n.observation, '') AS observation,
       coalesce(n.answer, '') AS answer,
       properties(n) AS props
LIMIT $node_limit
"""


# Second tree step: edges within the settled node set. Both endpoints carry a
# ``database`` filter; a missing ``condition`` property returns null, which the
# model's ``condition: str | None`` reads as "unconditional edge".
SOP_TREE_EDGES = f"""
MATCH (a)-[r:{REL_NEXT}]->(b)
WHERE a.{DB_PROPERTY} = $database AND b.{DB_PROPERTY} = $database
  AND a.id IN $node_ids AND b.id IN $node_ids
RETURN a.id AS source, b.id AS target, r.condition AS condition
ORDER BY source, target
"""
