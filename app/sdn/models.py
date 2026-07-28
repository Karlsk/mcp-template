"""Pydantic models for SDN controller responses (boundary validation).

Validate every controller response at the boundary with a typed model — this is
the "validate external data" rule. The skeleton ships only the health probe
model; add ``Device`` / ``Topology`` / etc. when wiring real GET endpoints.
"""

from __future__ import annotations

from pydantic import BaseModel


class SDNHealthResponse(BaseModel):
    """Structured result of a controller connectivity probe."""

    ok: bool
    status_code: int
    latency_ms: float


# TODO(sdn-wiring): add typed response models for real endpoints, e.g.
#   class Device(BaseModel):
#       id: str
#       name: str
#       kind: str | None = None
#       status: str | None = None
