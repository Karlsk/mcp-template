"""Live smoke test for search_sop against a real SOP graph (manual only).

Runs the full client + envelope chain against a real Neo4j:
probe -> resolve_sop_event -> get_sop_tree -> envelope JSON.

Usage:
    NEO4J_URI=bolt://HOST:7687 NEO4J_USERNAME=... NEO4J_PASSWORD='...' \
    PLAN_EVENT_NAME="..." PLAN_DATABASE="..." \
    PYTHONPATH=. uv run python scripts/test_sop_live.py
"""

from __future__ import annotations

import asyncio
import json
import os
import sys

from app.graph import GraphClient, GraphError, event_payload, node_to_json
from app.settings import Settings


async def main() -> int:
    event_name = os.getenv("PLAN_EVENT_NAME", "单圈次单落地星不通")
    db = os.getenv("PLAN_DATABASE", "xw_single_pass")
    settings = Settings()
    if not settings.neo4j_is_configured():
        print("FAIL: NEO4J_URI not set")
        return 1
    print(f"uri={settings.neo4j_base_uri}  db={db}  event_name={event_name!r}")

    graph = GraphClient(settings)
    try:
        ok = await graph.probe()
        print(f"[1] probe: {'OK' if ok else 'FAILED'}")
        if not ok:
            return 1

        found = await graph.resolve_sop_event(event_name)
        print(f"[2] resolve_sop_event -> {len(found)} hit(s)")
        for c in found:
            print(f"    db={c.db!r} event_name={c.event_name!r} "
                  f"fault_type={c.fault_type!r} intent={c.intent!r}")

        tree = await graph.get_sop_tree(db=db, event_name=event_name)
        if tree is None:
            print(f"[3] get_sop_tree({db!r}, {event_name!r}) -> None (no match)")
            return 1
        print(f"[3] get_sop_tree -> nodes={len(tree.nodes)} edges={len(tree.edges)} "
              f"truncated={tree.truncated}")

        payload = {
            "ok": True, "configured": True, "mode": "tree", "db": tree.db,
            "event": event_payload(tree.event),
            "nodes": [node_to_json(n) for n in tree.nodes],
            "edges": [e.model_dump() for e in tree.edges],
            "truncated": tree.truncated,
        }
        print("[4] envelope JSON:")
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        return 0
    except GraphError as exc:
        print(f"FAIL: GraphError: {exc} (detail={exc.detail})")
        return 1
    finally:
        await graph.aclose()


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
