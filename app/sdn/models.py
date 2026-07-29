"""Pydantic models for SDN controller responses (boundary validation).

Validate every controller response at the boundary with a typed model — this is
the "validate external data" rule. The skeleton ships only the health probe
model; add ``Device`` / ``Topology`` / etc. when wiring real GET endpoints.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field, model_validator


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


# --- v1.5 endpoint models (appended; existing models above are unchanged) ----
#
# Boundary models for the controller endpoints wired in this revision. Following
# the "thin typing" rule, only fields whose JSON type is certain are typed; every
# other field the controller returns is preserved via ``extra="allow"`` and can be
# promoted to a typed field after live validation. ``model_dump(by_alias=True)``
# round-trips the controller's kebab/camel vocabulary.


_SENSITIVE_KEYS = frozenset({"password", "community"})


def _strip_sensitive(obj: Any) -> Any:
    """Recursively drop credential keys (``password``, ``community``) and return a
    new structure, leaving the input untouched.

    Used as a ``mode="before"`` validator on models whose controller response
    carries in-band credentials (e.g. §2.2 device ``password``), so secrets can
    never reach the MCP/model layer.
    """
    if isinstance(obj, dict):
        return {k: _strip_sensitive(v) for k, v in obj.items() if k not in _SENSITIVE_KEYS}
    if isinstance(obj, list):
        return [_strip_sensitive(item) for item in obj]
    return obj


class PageResponse[T: BaseModel](BaseModel):
    """Generic Spring-Data page envelope (used by the paged device/link endpoints)."""

    model_config = ConfigDict(extra="allow")

    content: list[T] = Field(default_factory=list)
    total_elements: int = 0
    total_pages: int = 0
    last: bool = False
    first: bool = False
    size: int = 0
    number: int = 0
    number_of_elements: int = 0
    empty: bool = True


class PopInfo(BaseModel):
    model_config = ConfigDict(extra="allow", populate_by_name=True)

    id: str = ""
    name: str = ""
    province: str = ""
    city: str = ""


class DeviceLabel(BaseModel):
    model_config = ConfigDict(extra="allow", populate_by_name=True)

    id: str = ""
    name: str = ""


class Device(BaseModel):
    """A §2.2 PE device. Credentials are scrubbed before validation."""

    model_config = ConfigDict(extra="allow", populate_by_name=True)

    id: str = ""
    name: str = ""
    pe_alias: str | None = Field(None, alias="pe-alias")
    node_type: str | None = Field(None, alias="node-type")
    vendor_id: str | None = Field(None, alias="vendor-id")
    platform_id: str | None = Field(None, alias="platform-id")
    product_name: str | None = Field(None, alias="product-name")
    version: str | None = None
    management_ip: str | None = Field(None, alias="management-ip")
    connect_status: str | None = Field(None, alias="connect-status")
    te_loopback_if: str | None = Field(None, alias="te-loopback-if")
    te_loopback_ip: str | None = Field(None, alias="te-loopback-ip")
    te_loopback_ipv6: str | None = Field(None, alias="te-loopback-ipv6")
    bgp_loopback_if: str | None = Field(None, alias="bgp-loopback-if")
    bgp_loopback_ip: str | None = Field(None, alias="bgp-loopback-ip")
    bgp_loopback_ipv6: str | None = Field(None, alias="bgp-loopback-ipv6")
    locator: str | None = None
    pop_id: str | None = Field(None, alias="pop-id")
    create_time: str | None = Field(None, alias="create-time")
    update_time: str | None = Field(None, alias="update-time")
    pop_info: PopInfo | None = Field(None, alias="popInfo")
    label: list[DeviceLabel] = Field(default_factory=list)

    @model_validator(mode="before")
    @classmethod
    def _scrub_credentials(cls, data: Any) -> Any:
        return _strip_sensitive(data)


class LinkSource(BaseModel):
    model_config = ConfigDict(extra="allow", populate_by_name=True)

    source_node: str | None = Field(None, alias="source-node")
    source_node_alias: str | None = Field(None, alias="source-node-alias")
    source_tp: str | None = Field(None, alias="source-tp")
    source_tp_alias: str | None = Field(None, alias="source-tp-alias")


class LinkDestination(BaseModel):
    model_config = ConfigDict(extra="allow", populate_by_name=True)

    dest_node: str | None = Field(None, alias="dest-node")
    dest_node_alias: str | None = Field(None, alias="dest-node-alias")
    dest_tp: str | None = Field(None, alias="dest-tp")


class LinkInfo(BaseModel):
    """A §2.16 link entry."""

    model_config = ConfigDict(extra="allow", populate_by_name=True)

    link_id: str = Field("", alias="link-id")
    link_status: str | None = Field(None, alias="link-status")
    link_type: str | None = Field(None, alias="link-type")
    source_ip: str | None = Field(None, alias="source-ip")
    dest_ip: str | None = Field(None, alias="dest-ip")
    source_node_ip: str | None = Field(None, alias="source-node-ip")
    dest_node_ip: str | None = Field(None, alias="dest-node-ip")
    srv6_locator: str | None = Field(None, alias="srv6-locator")
    source: LinkSource | None = None
    destination: LinkDestination | None = None


class TerminationPoint(BaseModel):
    model_config = ConfigDict(extra="allow", populate_by_name=True)

    tp_id: str = Field("", alias="tp-id")
    tp_status: str | None = Field(None, alias="tp-status")
    type: str | None = None
    port_type: str | None = Field(None, alias="port-type")
    ip: str | None = None


class TopologyNode(BaseModel):
    model_config = ConfigDict(extra="allow", populate_by_name=True)

    node_id: str = Field("", alias="node-id")
    name: str = ""
    node_type: str | None = Field(None, alias="node-type")
    node_status: str | None = Field(None, alias="node-status")
    vendor_id: str | None = Field(None, alias="vendor-id")
    platform_id: str | None = Field(None, alias="platform-id")
    management_ip: str | None = Field(None, alias="management-ip")
    te_loopback_if: str | None = Field(None, alias="te-loopback-if")
    te_loopback_ip: str | None = Field(None, alias="te-loopback-ip")
    bgp_loopback_if: str | None = Field(None, alias="bgp-loopback-if")
    bgp_loopback_ip: str | None = Field(None, alias="bgp-loopback-ip")
    plane_type: str | None = Field(None, alias="plane-type")
    city: str | None = None
    version: str | None = None
    igp_router_id: str | None = Field(None, alias="igp-router-id")
    termination_point: list[TerminationPoint] = Field(
        default_factory=list, alias="termination-point"
    )


class TopologyLink(BaseModel):
    model_config = ConfigDict(extra="allow", populate_by_name=True)

    link_id: str = Field("", alias="link-id")
    link_status: str | None = Field(None, alias="link-status")
    source: LinkSource | None = None
    destination: LinkDestination | None = None


class Topology(BaseModel):
    model_config = ConfigDict(extra="allow", populate_by_name=True)

    topology_id: str = Field("", alias="topology-id")
    node: list[TopologyNode] = Field(default_factory=list)
    link: list[TopologyLink] = Field(default_factory=list)


class TopologyResponse(BaseModel):
    """§3.7 full topology payload."""

    model_config = ConfigDict(extra="allow")

    topology: list[Topology] = Field(default_factory=list)


class PerfDataPoint(BaseModel):
    """One sampled performance point; metrics vary by namespace and ride extras."""

    model_config = ConfigDict(extra="allow", populate_by_name=True)

    time: str = ""


class PerfHistoryResponse(BaseModel):
    """§3.2/§3.3/§3.4 history envelope (covers both observed shapes)."""

    model_config = ConfigDict(extra="allow")

    data: list[PerfDataPoint] = Field(default_factory=list)
    code: int = 0
    message: str = ""
    success: bool | None = None


class OperationLog(BaseModel):
    """One §3.12 system operation log entry."""

    model_config = ConfigDict(extra="allow", populate_by_name=True)

    id: str = ""
    time: str = ""
    account_name: str = Field("", alias="accountName")
    behave_as: str = Field("", alias="behaveAs")
    caller: str = ""
    method: str = ""
    url: str = ""
    operation_desc: str = Field("", alias="operationDesc")


class OperationLogsResponse(BaseModel):
    """§3.12 paged operation-log envelope."""

    model_config = ConfigDict(extra="allow")

    code: int = 0
    message: str = ""
    data: list[OperationLog] = Field(default_factory=list)


class CommandResultResponse(BaseModel):
    """Result of a device CLI command (POST /api/no/config/device-conf/command-result).

    The endpoint is not in the v1.5 spec; ``result`` holds the raw device output
    text (line endings preserved verbatim). Other fields, if any, ride extras.
    """

    model_config = ConfigDict(extra="allow")

    result: str = ""
