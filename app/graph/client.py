"""SOP graph business layer over the generic Neo4j client.

Owns endpoint concerns the generic driver must not know: the ``GraphError``
mapping (the ONLY place driver exceptions are handled), skeleton-mode guards,
and — from spec-03 on — the SOP query methods. Business methods follow one
uniform shape::

    async def some_query(self, ...) -> SomeModel:
        self._require_configured()
        try:
            rows = await self._neo4j.run_read(CYPHER_X, {...}, db_tag=db,
                                              query_name="some_query")
        except Exception as exc:                      # driver exceptions only here
            raise _map_neo4j_error(exc) from exc
        ...

``except Exception`` (rather than ``neo4j.exceptions.Neo4jError``) is deliberate:
the driver also raises ``asyncio.TimeoutError`` / ``OSError``, and every one of
them must be mapped onto a sanitized message.
"""

from __future__ import annotations

import logging
from typing import Any, Final

import neo4j
from neo4j import AsyncDriver

from app.common.neo4j import Neo4jClient, Neo4jClientConfig
from app.graph.cypher import (
    FIND_EVENTS_EXACT,
    FIND_EVENTS_FUZZY,
    LABEL_EVENT,
    LABEL_OUTPUT,
    LABEL_STEP,
    MAX_SOP_DEPTH,
    MAX_SOP_NODES,
    REL_SEQUENCE,
    RESOLVE_EVENT_BY_NAME,
    SOP_TREE_EDGES,
    sop_tree_nodes,
)
from app.graph.exceptions import (
    GraphAuthError,
    GraphConfigError,
    GraphConnectionError,
    GraphError,
    GraphQueryError,
)
from app.graph.models import SOPCandidate, SOPEdge, SOPNode, SOPTree
from app.settings import Settings

logger = logging.getLogger(__name__)

_KNOWN_LABELS = frozenset({LABEL_EVENT, LABEL_STEP, LABEL_OUTPUT})

# Synthetic Output node for dangling paths (spec-05 §3.4: dead-end nodes get
# a unified "no conclusion" Output so the tree is always fully terminated).
_SYNTHETIC_OUTPUT_ID: Final = "__no_conclusion__"
_SYNTHETIC_OUTPUT_NAME: Final = "未终结"
_SYNTHETIC_OUTPUT_REASON: Final = "该路径无明确结论，大模型根据上下文进行总结"  # noqa: RUF001


def _normalize(value: str | None) -> str | None:
    """Case-insensitive matching starts client-side: strip + lower; empty -> None."""
    if value is None:
        return None
    cleaned = value.strip().lower()
    return cleaned or None


def _kind_from_labels(labels: object) -> str:
    """Map node labels onto ``SOPNode.kind`` (unknown labels stay, lowercased)."""
    for label in labels if isinstance(labels, list) else []:
        if label in _KNOWN_LABELS:
            return str(label).lower()
    if isinstance(labels, list) and labels:
        return str(labels[0]).lower()
    return ""


def _detect_dangling_nodes(nodes: list[SOPNode], edges: list[SOPEdge]) -> list[SOPNode]:
    """Nodes with no outgoing edges that are not Output nodes.

    These are dead-end paths that never reach a conclusion; a synthetic Output
    is appended to keep the tree semantically complete.
    """
    sources = {e.source for e in edges}
    return [n for n in nodes if n.id not in sources and n.kind != "output"]


def _label_from_labels(labels: object) -> str:
    """First label verbatim — the envelope echoes it as ``label`` (spec-05 §5)."""
    if isinstance(labels, list) and labels:
        return str(labels[0])
    return ""


def _to_node(row: dict[str, Any]) -> SOPNode:
    """Build a SOPNode from one sop_tree_nodes row.

    Typed fields come from the coalesced RETURN columns (the dual-case
    reconciliation already happened in Cypher, spec-05 §3.1); ``props`` is
    merged afterwards so schema evolution survives via ``extra="allow"``
    (typed values win over raw props).
    """
    data: dict[str, Any] = {
        "id": row.get("id") or "",
        "kind": _kind_from_labels(row.get("labels")),
        "label": _label_from_labels(row.get("labels")),
        "name": row.get("name") or "",
        "action": row.get("action") or "",
        "observation": row.get("observation") or "",
        "reason": row.get("final_answer") or "",
    }
    props = row.get("props")
    if isinstance(props, dict):
        for key, value in props.items():
            data.setdefault(key, value)
    return SOPNode.model_validate(data)


def _map_neo4j_error(exc: Exception) -> GraphError:
    """Map a driver exception onto the sanitized graph error hierarchy.

    Order matters: ``AuthError`` is a ``ClientError`` subclass and must be
    checked first; ``Neo4jError`` (the base) is covered by the fallback.
    """
    if isinstance(exc, neo4j.exceptions.AuthError):
        return GraphAuthError("SOP graph authentication failed.", detail=str(exc))
    if isinstance(
        exc, neo4j.exceptions.ServiceUnavailable | neo4j.exceptions.SessionExpired
    ):
        return GraphConnectionError("SOP graph is unreachable.", detail=str(exc))
    if isinstance(exc, neo4j.exceptions.ClientError):
        return GraphQueryError("SOP graph rejected the query.", detail=str(exc))
    return GraphError("SOP graph request failed.", detail=str(exc))


class GraphClient:
    """SOP graph business methods over the generic Neo4j client."""

    def __init__(self, settings: Settings, *, driver: AsyncDriver | None = None) -> None:
        self._settings = settings
        self._neo4j = Neo4jClient(
            Neo4jClientConfig(
                uri=settings.neo4j_base_uri,
                username=settings.neo4j_username or "",
                password=(
                    settings.neo4j_password.get_secret_value()
                    if settings.neo4j_password
                    else ""
                ),
                database=settings.neo4j.database,
                query_timeout=settings.neo4j.query_timeout,
                max_transaction_retry_time=settings.neo4j.max_transaction_retry_time,
                log_params=settings.neo4j.log_params,
                driver=driver,
            )
        )

    @property
    def configured(self) -> bool:
        return self._neo4j.configured

    def _require_configured(self) -> None:
        if not self.configured:
            raise GraphConfigError("SOP graph is not configured.")

    async def probe(self) -> bool:
        """Best-effort connectivity check at startup. Never raises.

        Unlike the SDN basic-auth login this is NOT fail-fast: SDN tools must
        keep working when the SOP graph is down (spec-02 decision 7).
        """
        if not self.configured:
            return False
        try:
            await self._neo4j.verify_connectivity()
        except Exception as exc:
            logger.warning(
                "graph_probe_failed", extra={"error_type": type(exc).__name__}
            )
            return False
        return True

    async def aclose(self) -> None:
        """Close the underlying driver (idempotent)."""
        await self._neo4j.close()

    # ------------------------------------------------------------------
    # SOP business methods (spec-03 §5). Uniform shape: guard, run, map.
    # ------------------------------------------------------------------

    async def _run(
        self,
        statement: str,
        params: dict[str, Any] | None,
        *,
        db_tag: str | None,
        allow_cross_db: bool = False,
        query_name: str = "query",
    ) -> list[dict[str, Any]]:
        """Thin wrapper: the ONLY place driver exceptions are mapped."""
        try:
            return await self._neo4j.run_read(
                statement,
                params,
                db_tag=db_tag,
                allow_cross_db=allow_cross_db,
                query_name=query_name,
            )
        except Exception as exc:  # driver exceptions only here
            raise _map_neo4j_error(exc) from exc

    async def find_sop_events(
        self,
        *,
        fault_type: str | None = None,
        intent: str | None = None,
        keyword: str | None = None,
        db: str | None = None,
        limit: int = 10,
    ) -> tuple[list[SOPCandidate], str]:
        """Find candidate SOP Events. Returns (candidates, match_mode).

        ``match_mode`` is "exact", "fuzzy", or "none". Discovery spans every logical
        database unless ``db`` narrows it — the only cross-db query in the codebase.
        """
        self._require_configured()
        needle = _normalize(keyword) or _normalize(fault_type) or _normalize(intent)
        params: dict[str, Any] = {
            # Cypher references $database even on the cross-db path, so the
            # parameter must exist (as null) or Neo4j raises ParameterMissing.
            # When ``db`` narrows the search, run_read overwrites this preset
            # with the tag.
            "database": None,
            "fault_type": _normalize(fault_type),
            "intent": _normalize(intent),
            "needle": needle,
            "limit": limit,
        }
        cross = db is None
        rows = await self._run(
            FIND_EVENTS_EXACT, params, db_tag=db, allow_cross_db=cross,
            query_name="find_sop_events_exact",
        )
        mode = "exact"
        if not rows and needle:
            rows = await self._run(
                FIND_EVENTS_FUZZY, params, db_tag=db, allow_cross_db=cross,
                query_name="find_sop_events_fuzzy",
            )
            mode = "fuzzy"
        if not rows:
            mode = "none"
        return [SOPCandidate.model_validate(row) for row in rows], mode

    async def resolve_sop_event(self, event_name: str) -> list[SOPCandidate]:
        """Reverse-lookup an Event by name across logical databases.

        Used when the caller supplies an event name without ``db``. Names are
        not guaranteed globally unique, so multiple hits are returned rather
        than guessed (spec-05 §3.2: locating always goes through the name).
        """
        self._require_configured()
        rows = await self._run(
            RESOLVE_EVENT_BY_NAME,
            # Same ParameterMissing contract as the discovery stage.
            {"database": None, "event_name": _normalize(event_name) or event_name},
            db_tag=None,
            allow_cross_db=True,
            query_name="resolve_sop_event",
        )
        return [SOPCandidate.model_validate(row) for row in rows]

    async def get_sop_tree(
        self, *, db: str, event_name: str, max_depth: int = MAX_SOP_DEPTH
    ) -> SOPTree | None:
        """Fetch one complete SOP tree, scoped to a single logical database.

        Returns None when no Event matches ``(db, event_name)``. Traversal is
        confined to ``db``: every node AND every relationship on every path
        must carry the same ``database`` property (spec-05 §1.2).
        """
        self._require_configured()
        depth = max(1, min(int(max_depth), MAX_SOP_DEPTH))
        rows = await self._run(
            sop_tree_nodes(depth),
            {"event_name": event_name, "node_limit": MAX_SOP_NODES + 1},
            db_tag=db,
            query_name="sop_tree_nodes",
        )
        if not rows:
            return None
        truncated = len(rows) > MAX_SOP_NODES
        rows = rows[:MAX_SOP_NODES]
        nodes = [_to_node(row) for row in rows]
        edge_rows = await self._run(
            SOP_TREE_EDGES,
            {"node_ids": [n.id for n in nodes]},
            db_tag=db,
            query_name="sop_tree_edges",
        )
        edges = [SOPEdge.model_validate(r) for r in edge_rows]
        event = next((n for n in nodes if n.kind == "event"), None)
        if event is None:
            raise GraphQueryError(
                "SOP graph response was malformed.",
                detail=f"no Event node in tree rows for {db}/{event_name}",
            )
        dangling = _detect_dangling_nodes(nodes, edges)
        if dangling:
            synthetic = SOPNode.model_validate({
                "id": _SYNTHETIC_OUTPUT_ID,
                "kind": "output",
                "name": _SYNTHETIC_OUTPUT_NAME,
                "reason": _SYNTHETIC_OUTPUT_REASON,
                "label": LABEL_OUTPUT,
            })
            nodes.append(synthetic)
            for dn in dangling:
                edges.append(
                    SOPEdge(
                        source=dn.id,
                        target=_SYNTHETIC_OUTPUT_ID,
                        rel_type=REL_SEQUENCE,
                    )
                )
        return SOPTree(
            db=db,
            event=event,
            nodes=nodes,
            edges=edges,
            truncated=truncated,
        )
