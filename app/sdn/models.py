"""Pydantic models for SDN controller responses (boundary validation).

Validate every controller response at the boundary with a typed model — this is
the "validate external data" rule. The skeleton ships only the health probe
model; add ``Device`` / ``Topology`` / etc. when wiring real GET endpoints.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field


class SDNHealthResponse(BaseModel):
    """Structured result of a controller connectivity probe."""

    ok: bool
    status_code: int
    latency_ms: float


class SDNAlert(BaseModel):
    """A single device alert, boundary-validated.

    Known fields are typed; ``extra="allow"`` preserves every other field the
    controller returns (its alert schema is wide and evolves), so no data is
    dropped while the stable keys stay typed.
    """

    model_config = ConfigDict(extra="allow")

    id: str = ""
    category: str = ""
    source: str = ""
    component: str = ""
    level: str = ""
    msg: str = ""
    time: str = ""


class SDNAlertsResponse(BaseModel):
    """Structured result of a paged alerts query (the controller's envelope)."""

    total: int = 0
    success: bool = False
    message: str = ""
    code: int = 0
    data: list[SDNAlert] = Field(default_factory=list)


# TODO(sdn-wiring): add typed response models for real endpoints, e.g.
#   class Device(BaseModel):
#       id: str
#       name: str
#       kind: str | None = None
#       status: str | None = None
