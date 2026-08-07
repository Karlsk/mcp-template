"""SOP graph controlled retrieval tool.

``search_sop`` implements the two-stage retrieval defined in
docs/spec-03-search_sop-SOP图检索.md: a cross-db discovery stage followed by
a tree expansion locked to one logical db. Parameter names stay frozen from
spec-01; only the body changed.
"""

from __future__ import annotations

import logging
from typing import Any

from mcp.server.fastmcp import Context, FastMCP

from app.graph import (
    GraphClient,
    GraphError,
    SOPCandidate,
    SOPTree,
    event_payload,
    node_to_json,
)
from app.graph.cypher import MAX_SOP_CANDIDATES, MAX_SOP_DEPTH
from app.tools.validation import (
    graph_skeleton_payload,
    positive_bound_detail,
    unexpected_payload,
)

logger = logging.getLogger(__name__)


def _tree_payload(tree: SOPTree | None, match: str) -> dict[str, object]:
    if tree is None:
        return {"ok": False, "configured": True,
                "detail": "no SOP event matches the given db and event_name"}
    return {
        "ok": True,
        "configured": True,
        "mode": "tree",
        "match": match,
        "db": tree.db,
        # spec-05 §5 envelope: derived node type/label, name-based event root.
        "event": event_payload(tree.event),
        "nodes": [node_to_json(n) for n in tree.nodes],
        "edges": [e.model_dump() for e in tree.edges],
        "truncated": tree.truncated,
        "next_step_hint": (
            "Each Step's 'action' is the key for search_command_template."
        ),
    }


def _candidates_payload(candidates: list[SOPCandidate], match: str) -> dict[str, object]:
    return {
        "ok": True,
        "configured": True,
        "mode": "candidates",
        "match": match,
        "candidates": [c.model_dump() for c in candidates],
        "next_step_hint": "Re-call search_sop with both db and event_name of one candidate.",
    }


def _empty_payload(match: str) -> dict[str, object]:
    return {
        "ok": True,
        "configured": True,
        "mode": "empty",
        "match": match,
        "candidates": [],
        "detail": "no SOP event matched; try a broader keyword or a different fault_type",
    }


def register(mcp: FastMCP) -> None:
    """Register the SOP graph search tool."""

    @mcp.tool(
        name="search_sop",
        description=(
            "Search the SOP graph for the standard operating procedure matching a "
            "fault type or intent, and return its full decision tree. Matching is "
            "exact-first (name/alias/fault_type/intent), falling back to keyword "
            "substring. One hit returns the tree (mode='tree'); several hits return "
            "candidates (mode='candidates') — re-call with the candidate's db AND "
            "event_name to expand one. Tree nodes carry type 'function_call' "
            "(Steps, with an 'action' key: feed it to search_command_template to "
            "get the vendor-specific command) or 'final_answer' (conclusion in "
            "'reason'). Returns "
            "{ok, configured, mode, match, db, event, nodes[], edges[], truncated}."
        ),
    )
    async def search_sop(
        ctx: Context[Any, Any, Any],
        fault_type: str | None = None,
        intent: str | None = None,
        keyword: str | None = None,
        db: str | None = None,
        event_id: str | None = None,
        limit: int = 10,
        max_depth: int = MAX_SOP_DEPTH,
    ) -> dict[str, object]:
        if not any((fault_type, intent, keyword, event_id)):
            return {
                "ok": False,
                "detail": "provide at least one of fault_type / intent / keyword / event_id",
            }
        if detail := positive_bound_detail("limit", limit, MAX_SOP_CANDIDATES):
            return {"ok": False, "detail": detail}
        if detail := positive_bound_detail("max_depth", max_depth, MAX_SOP_DEPTH):
            return {"ok": False, "detail": detail}

        try:
            graph: GraphClient = ctx.request_context.lifespan_context["graph_client"]
            if not graph.configured:
                return graph_skeleton_payload()

            # Direct expansion: (db, event_name) locates exactly one tree.
            # The frozen tool parameter ``event_id`` carries the Event name
            # (spec-05 §3.2: locating always goes through the name).
            if event_id and db:
                return _tree_payload(
                    await graph.get_sop_tree(
                        db=db, event_name=event_id, max_depth=max_depth
                    ),
                    "exact",
                )

            # event_id without db: reverse-lookup, expand only when unambiguous.
            if event_id:
                found = await graph.resolve_sop_event(event_id)
                if len(found) == 1:
                    return _tree_payload(
                        await graph.get_sop_tree(
                            db=found[0].db, event_name=event_id, max_depth=max_depth
                        ),
                        "exact",
                    )
                if not found:
                    return _empty_payload("exact")
                return _candidates_payload(found, "exact")

            candidates, match = await graph.find_sop_events(
                fault_type=fault_type,
                intent=intent,
                keyword=keyword,
                db=db,
                limit=limit,
            )
            if not candidates:
                return _empty_payload(match)
            if len(candidates) == 1:
                only = candidates[0]
                return _tree_payload(
                    await graph.get_sop_tree(
                        db=only.db, event_name=only.event_name, max_depth=max_depth
                    ),
                    match,
                )
            return _candidates_payload(candidates, match)
        except GraphError as exc:
            await ctx.error(f"search_sop failed: {exc}")
            return {"ok": False, "configured": True, "detail": str(exc)}
        except Exception:
            logger.exception("search_sop unexpected failure")
            return unexpected_payload()
