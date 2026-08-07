"""Envelope pure functions for the search_sop JSON shape (spec-05 §5).

No IO, no imports beyond the graph models. Mirrors the reference
implementation's ``determine_node_type`` / ``build_node_object`` rules:

- ``type`` is DERIVED, never stored: Output nodes (or anything carrying a
  non-empty ``reason``) become ``final_answer``; everything else is a
  ``function_call``.
- ``label`` echoes the first graph label verbatim.
- Empty optional strings are omitted; unknown props (kept on the model via
  ``extra="allow"``) never leak into the envelope.
"""

from __future__ import annotations

from typing import Any

from app.graph.models import SOPNode


def node_type(node: SOPNode) -> str:
    """Derive the envelope ``type`` (``final_answer`` vs ``function_call``)."""
    if node.kind == "output" or node.reason:
        return "final_answer"
    return "function_call"


def node_to_json(node: SOPNode) -> dict[str, Any]:
    """One tree node -> its envelope dict (fixed keys, empty strings omitted)."""
    extra = node.model_extra or {}
    payload: dict[str, Any] = {
        "id": node.id,
        "name": node.name,
        "type": node_type(node),
        "label": str(extra.get("label", "") or ""),
    }
    if payload["type"] == "final_answer":
        if node.reason:
            payload["reason"] = node.reason
    else:
        if node.action:
            payload["action"] = node.action
        if node.observation:
            payload["observation"] = node.observation
    return payload


def event_payload(event: SOPNode) -> dict[str, Any]:
    """The Event root -> ``{event_name, event_id, fault_type, intent}``.

    ``fault_type``/``intent`` ride along as extra props (``extra="allow"``)
    when the Event node carries them; missing values default to ``""``.
    """
    extra = event.model_extra or {}
    return {
        "event_name": event.name,
        "event_id": event.id,
        "fault_type": str(extra.get("fault_type", "") or ""),
        "intent": str(extra.get("intent", "") or ""),
    }
