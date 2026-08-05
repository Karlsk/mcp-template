"""SOP graph labels, relationship types, and Cypher statement constants.

Aligning with the real graph (should label naming differ) only touches this
file — never the client logic. Statement bodies follow spec-03 §4.
"""

from __future__ import annotations

from typing import Final

LABEL_EVENT: Final = "Event"
LABEL_STEP: Final = "Step"
LABEL_OUTPUT: Final = "Output"
REL_NEXT: Final = "NEXT"
DB_PROPERTY: Final = "_db"

MAX_SOP_DEPTH: Final = 20      # variable-length traversal cap (cycle guard)
MAX_SOP_NODES: Final = 200     # node budget per tree
MAX_SOP_CANDIDATES: Final = 50 # upper bound for the `limit` tool parameter

# -- Discovery stage (spec-03 §4.1) ------------------------------------------
# ``($_db IS NULL OR ...)``: callers MUST pass an explicit ``{"_db": None}``
# on the cross-db path — Cypher referencing a parameter that was never sent is
# rejected by Neo4j (ParameterMissing).
FIND_EVENTS_EXACT = f"""
MATCH (e:{LABEL_EVENT})
WHERE ($_db IS NULL OR e.{DB_PROPERTY} = $_db)
  AND (
    ($fault_type IS NOT NULL AND toLower(e.fault_type) = $fault_type)
    OR ($intent IS NOT NULL AND toLower(e.intent) = $intent)
    OR ($needle IS NOT NULL AND toLower(e.name) = $needle)
    OR ($needle IS NOT NULL AND $needle IN [a IN coalesce(e.aliases, []) | toLower(a)])
  )
RETURN e.{DB_PROPERTY} AS db, e.id AS event_id, e.name AS name,
       coalesce(e.fault_type, '') AS fault_type, coalesce(e.intent, '') AS intent
ORDER BY db, event_id
LIMIT $limit
"""

# Fuzzy degradation (spec-03 §4.2): only issued when the exact stage found
# nothing — the two semantics ("is it" vs "might be it") stay separate.
FIND_EVENTS_FUZZY = f"""
MATCH (e:{LABEL_EVENT})
WHERE ($_db IS NULL OR e.{DB_PROPERTY} = $_db)
  AND $needle IS NOT NULL
  AND (
    toLower(coalesce(e.name, '')) CONTAINS $needle
    OR toLower(coalesce(e.fault_type, '')) CONTAINS $needle
    OR toLower(coalesce(e.intent, '')) CONTAINS $needle
    OR toLower(coalesce(e.description, '')) CONTAINS $needle
    OR ANY(a IN coalesce(e.aliases, []) WHERE toLower(a) CONTAINS $needle)
  )
RETURN e.{DB_PROPERTY} AS db, e.id AS event_id, e.name AS name,
       coalesce(e.fault_type, '') AS fault_type, coalesce(e.intent, '') AS intent
ORDER BY db, event_id
LIMIT $limit
"""

# Reverse lookup by id across logical databases (spec-03 §5 resolve_sop_event).
RESOLVE_EVENT_BY_ID = f"""
MATCH (e:{LABEL_EVENT})
WHERE ($_db IS NULL OR e.{DB_PROPERTY} = $_db)
  AND e.id = $event_id
RETURN e.{DB_PROPERTY} AS db, e.id AS event_id, e.name AS name,
       coalesce(e.fault_type, '') AS fault_type, coalesce(e.intent, '') AS intent
ORDER BY db, event_id
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
WHERE e.{DB_PROPERTY} = $_db AND e.id = $event_id
OPTIONAL MATCH path = (e)-[:{REL_NEXT}*1..{max_depth}]->(m)
WHERE ALL(n IN nodes(path) WHERE n.{DB_PROPERTY} = $_db)
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
# ``_db`` filter; a missing ``condition`` property returns null, which the
# model's ``condition: str | None`` reads as "unconditional edge".
SOP_TREE_EDGES = f"""
MATCH (a)-[r:{REL_NEXT}]->(b)
WHERE a.{DB_PROPERTY} = $_db AND b.{DB_PROPERTY} = $_db
  AND a.id IN $node_ids AND b.id IN $node_ids
RETURN a.id AS source, b.id AS target, r.condition AS condition
ORDER BY source, target
"""
