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


class GraphFragment(BaseModel):
    """Serialization-neutral graph payload: plain nodes + edges.

    Deliberately not a networkx graph — see docs/spec-02 §11. Keeping this
    shape means a NetworkX adapter can be added later in ``app/graph/nx.py``
    without touching the client or the tools.
    """

    nodes: list[dict[str, Any]] = Field(default_factory=list)
    edges: list[SOPEdge] = Field(default_factory=list)
    truncated: bool = False
