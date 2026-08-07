"""Tests for the SOP graph integration layer (app/graph/)."""

from __future__ import annotations

import logging

import pytest
from neo4j.exceptions import AuthError, ClientError, ServiceUnavailable, SessionExpired

from app.graph import (
    GraphAuthError,
    GraphClient,
    GraphConfigError,
    GraphConnectionError,
    GraphError,
    GraphQueryError,
)
from app.graph.client import _map_neo4j_error
from app.graph.cypher import (
    DB_PROPERTY,
    LABEL_EVENT,
    LABEL_OUTPUT,
    LABEL_STEP,
    REL_NEXT,
)
from app.graph.models import GraphFragment, SOPEdge
from app.settings import Neo4jSettings, Settings


def make_settings(uri: str = "bolt://graph.example:7687") -> Settings:
    return Settings(
        neo4j=Neo4jSettings(uri=uri, query_timeout=1.0, max_transaction_retry_time=0.0)
    )


class ProbeDriver:
    """Minimal fake driver: only the surface probe()/aclose() touch."""

    def __init__(self, connectivity_exc: Exception | None = None) -> None:
        self.connectivity_exc = connectivity_exc
        self.close_count = 0

    async def verify_connectivity(self) -> None:
        if self.connectivity_exc is not None:
            raise self.connectivity_exc

    async def close(self) -> None:
        self.close_count += 1


# ---------------------------------------------------------------------------
# Error mapping order + sanitization
# ---------------------------------------------------------------------------


def test_map_auth_error_wins_over_client_error() -> None:
    """AuthError is a ClientError subclass — it must be checked first."""
    exc = AuthError("bad credentials for bolt://secret-host:7687")
    mapped = _map_neo4j_error(exc)
    assert isinstance(mapped, GraphAuthError)
    assert str(mapped) == "SOP graph authentication failed."


def test_map_service_unavailable() -> None:
    mapped = _map_neo4j_error(ServiceUnavailable("bolt://host:7687 is down"))
    assert isinstance(mapped, GraphConnectionError)
    assert str(mapped) == "SOP graph is unreachable."


def test_map_session_expired() -> None:
    mapped = _map_neo4j_error(SessionExpired("session expired on bolt://host"))
    assert isinstance(mapped, GraphConnectionError)


def test_map_client_error() -> None:
    mapped = _map_neo4j_error(ClientError("Neo.ClientError.Statement.SyntaxError"))
    assert isinstance(mapped, GraphQueryError)
    assert str(mapped) == "SOP graph rejected the query."


@pytest.mark.parametrize("exc", [OSError("conn reset"), TimeoutError()])
def test_map_fallback(exc: Exception) -> None:
    mapped = _map_neo4j_error(exc)
    assert type(mapped) is GraphError
    assert str(mapped) == "SOP graph request failed."


def test_mapped_errors_never_leak_raw_detail() -> None:
    leaks = [
        AuthError("bad credentials for bolt://secret-host:7687"),
        ServiceUnavailable("bolt://secret-host:7687 unreachable"),
        ClientError("Neo.ClientError.Statement.SyntaxError near MATCH"),
        OSError("connection reset by peer fd=7"),
    ]
    for exc in leaks:
        mapped = _map_neo4j_error(exc)
        assert str(exc) not in str(mapped)
        assert mapped.detail == str(exc)


# ---------------------------------------------------------------------------
# Skeleton mode
# ---------------------------------------------------------------------------


def test_skeleton_mode_not_configured() -> None:
    client = GraphClient(make_settings(uri=""))
    assert client.configured is False
    with pytest.raises(GraphConfigError, match="not configured"):
        client._require_configured()


def test_configured_with_uri() -> None:
    client = GraphClient(make_settings())
    assert client.configured is True
    client._require_configured()  # must not raise


def test_configured_with_injected_driver() -> None:
    client = GraphClient(make_settings(uri=""), driver=ProbeDriver())
    assert client.configured is True


# ---------------------------------------------------------------------------
# probe() never raises
# ---------------------------------------------------------------------------


async def test_probe_success() -> None:
    client = GraphClient(make_settings(), driver=ProbeDriver())
    assert await client.probe() is True


async def test_probe_failure_is_swallowed(caplog: pytest.LogCaptureFixture) -> None:
    driver = ProbeDriver(connectivity_exc=ServiceUnavailable("bolt://x down"))
    client = GraphClient(make_settings(), driver=driver)
    with caplog.at_level(logging.WARNING, logger="app.graph.client"):
        assert await client.probe() is False
    warnings = [r for r in caplog.records if r.message == "graph_probe_failed"]
    assert warnings
    assert warnings[0].levelno == logging.WARNING


async def test_probe_skeleton_mode_returns_false() -> None:
    client = GraphClient(make_settings(uri=""))
    assert await client.probe() is False


async def test_aclose_closes_driver() -> None:
    driver = ProbeDriver()
    client = GraphClient(make_settings(), driver=driver)
    await client.aclose()
    assert driver.close_count == 1


# ---------------------------------------------------------------------------
# Models + cypher constants
# ---------------------------------------------------------------------------


def test_cypher_constants() -> None:
    assert LABEL_EVENT == "Event"
    assert LABEL_STEP == "Step"
    assert LABEL_OUTPUT == "Output"
    assert REL_NEXT == "NEXT"
    assert DB_PROPERTY == "database"


def test_graph_fragment_is_serialization_neutral() -> None:
    fragment = GraphFragment(
        nodes=[{"id": "e1", "kind": "event"}],
        edges=[SOPEdge(source="e1", target="s1")],
        truncated=True,
    )
    dumped = fragment.model_dump()
    assert dumped["edges"] == [{"source": "e1", "target": "s1", "condition": None}]
    assert dumped["truncated"] is True


def test_sop_edge_keeps_unknown_fields() -> None:
    edge = SOPEdge.model_validate({"source": "a", "target": "b", "weight": 2})
    assert edge.model_dump()["weight"] == 2
