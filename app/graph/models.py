"""Pure data models for the SOP graph layer (no IO, no internal imports)."""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class SOPEdge(BaseModel):
    """A :NEXT edge. ``condition`` is None for a plain (unconditional) edge."""

    model_config = ConfigDict(extra="allow")

    source: str = ""
    target: str = ""
    condition: str | None = None


class SOPNode(BaseModel):
    """A node in a SOP tree (Event / Step / Output share this shape)."""

    model_config = ConfigDict(extra="allow")

    id: str = ""
    kind: str = ""            # "event" | "step" | "output" — derived from the label
    name: str = ""
    action: str = ""          # Step only: the command-template intent key
    observation: str = ""     # Step only: field to extract from the command output
    answer: str = ""          # Output only


class SOPCandidate(BaseModel):
    """One matched Event. ``db`` + ``event_id`` together locate its tree."""

    model_config = ConfigDict(extra="allow")

    db: str = ""
    event_id: str = ""
    name: str = ""
    fault_type: str = ""
    intent: str = ""


class SOPTree(BaseModel):
    """A complete SOP tree: one Event root, every reachable Step/Output, all edges."""

    model_config = ConfigDict(extra="allow")

    db: str = ""
    event: SOPNode
    nodes: list[SOPNode] = Field(default_factory=list)
    edges: list[SOPEdge] = Field(default_factory=list)
    truncated: bool = False


class GraphFragment(BaseModel):
    """Serialization-neutral graph payload: plain nodes + edges.

    Deliberately not a networkx graph — see docs/spec-02 §11. Keeping this
    shape means a NetworkX adapter can be added later in ``app/graph/nx.py``
    without touching the client or the tools.
    """

    nodes: list[dict[str, Any]] = Field(default_factory=list)
    edges: list[SOPEdge] = Field(default_factory=list)
    truncated: bool = False
