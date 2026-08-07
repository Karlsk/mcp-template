"""Tests for the generic Neo4j client (app/common/neo4j.py).

All tests run against a fake AsyncDriver — no real server is contacted.
"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from typing import Any

import pytest

from app.common.neo4j import Neo4jClient, Neo4jClientConfig

SCOPED_CYPHER = "MATCH (n) WHERE n.database = $database RETURN n"
UNSCOPED_CYPHER = "MATCH (n) RETURN n"


class FakeRecord:
    def __init__(self, data: dict[str, Any]) -> None:
        self._data = data

    def data(self) -> dict[str, Any]:
        return self._data


class FakeResult:
    def __init__(self, records: list[dict[str, Any]]) -> None:
        self._records = [FakeRecord(r) for r in records]

    def __aiter__(self) -> AsyncIterator[FakeRecord]:
        self._iter = iter(self._records)
        return self

    async def __anext__(self) -> FakeRecord:
        try:
            return next(self._iter)
        except StopIteration:
            raise StopAsyncIteration from None


class FakeTransaction:
    def __init__(self, driver: FakeDriver) -> None:
        self._driver = driver

    async def run(
        self, cypher: str, params: dict[str, Any], timeout: float | None = None
    ) -> FakeResult:
        self._driver.calls.append({"cypher": cypher, "params": params, "timeout": timeout})
        if self._driver.run_exc is not None:
            raise self._driver.run_exc
        return FakeResult(self._driver.records)


class FakeSession:
    def __init__(self, driver: FakeDriver, database: str | None) -> None:
        self._driver = driver
        self.database = database

    async def __aenter__(self) -> FakeSession:
        return self

    async def __aexit__(self, *exc_info: object) -> None:
        self._driver.sessions_closed += 1

    async def execute_read(
        self, work: Any, *args: object, **kwargs: object
    ) -> list[dict[str, Any]]:
        return await work(FakeTransaction(self._driver), *args, **kwargs)


class FakeDriver:
    def __init__(
        self,
        records: list[dict[str, Any]] | None = None,
        run_exc: Exception | None = None,
        connectivity_exc: Exception | None = None,
    ) -> None:
        self.records = records or []
        self.run_exc = run_exc
        self.connectivity_exc = connectivity_exc
        self.calls: list[dict[str, Any]] = []
        self.session_databases: list[str | None] = []
        self.sessions_closed = 0
        self.close_count = 0

    def session(self, database: str | None = None) -> FakeSession:
        self.session_databases.append(database)
        return FakeSession(self, database)

    async def verify_connectivity(self) -> None:
        if self.connectivity_exc is not None:
            raise self.connectivity_exc

    async def close(self) -> None:
        self.close_count += 1


def make_client(driver: FakeDriver) -> Neo4jClient:
    return Neo4jClient(Neo4jClientConfig(uri="bolt://fake:7687", driver=driver))


# ---------------------------------------------------------------------------
# database logical-database guard (the core contract)
# ---------------------------------------------------------------------------


async def test_run_read_rejects_scoped_tag_without_db_filter() -> None:
    driver = FakeDriver()
    client = make_client(driver)
    with pytest.raises(ValueError, match=r"must filter on \$database"):
        await client.run_read(UNSCOPED_CYPHER, db_tag="sop")
    assert driver.calls == []  # never reached the driver


async def test_run_read_injects_db_tag_param() -> None:
    driver = FakeDriver(records=[{"name": "e1"}])
    client = make_client(driver)
    rows = await client.run_read(SCOPED_CYPHER, db_tag="sop")
    assert rows == [{"name": "e1"}]
    assert driver.calls[0]["params"]["database"] == "sop"


async def test_run_read_requires_explicit_cross_db() -> None:
    client = make_client(FakeDriver())
    with pytest.raises(ValueError, match="cross-logical-db"):
        await client.run_read(UNSCOPED_CYPHER, db_tag=None)


async def test_run_read_cross_db_does_not_inject_db() -> None:
    driver = FakeDriver(records=[])
    client = make_client(driver)
    await client.run_read(UNSCOPED_CYPHER, db_tag=None, allow_cross_db=True)
    assert "database" not in driver.calls[0]["params"]


async def test_run_read_cross_db_keeps_explicit_none_db() -> None:
    """Discovery-phase call form: params['database'] is None and must pass through."""
    driver = FakeDriver(records=[])
    client = make_client(driver)
    await client.run_read(
        SCOPED_CYPHER, {"database": None}, db_tag=None, allow_cross_db=True
    )
    params = driver.calls[0]["params"]
    assert "database" in params
    assert params["database"] is None


async def test_run_read_db_tag_overrides_caller_params() -> None:
    driver = FakeDriver(records=[])
    client = make_client(driver)
    await client.run_read(SCOPED_CYPHER, {"database": "other"}, db_tag="sop")
    assert driver.calls[0]["params"]["database"] == "sop"


async def test_run_read_legacy_db_param_is_just_a_plain_param() -> None:
    """Regression (spec-05 §2): the old `_db` key is no longer special-cased.

    A leftover `{"_db": ...}` preset must neither satisfy the guard nor be
    overwritten — it travels downstream as an ordinary parameter.
    """
    driver = FakeDriver(records=[])
    client = make_client(driver)
    with pytest.raises(ValueError, match=r"must filter on \$database"):
        await client.run_read(UNSCOPED_CYPHER, {"_db": "sop"}, db_tag="sop")
    rows_driver = FakeDriver(records=[])
    await make_client(rows_driver).run_read(
        SCOPED_CYPHER, {"_db": "legacy"}, db_tag="sop"
    )
    params = rows_driver.calls[0]["params"]
    assert params["_db"] == "legacy"  # untouched, not overwritten
    assert params["database"] == "sop"  # the scope still comes from db_tag


# ---------------------------------------------------------------------------
# Injection seam, configuration and lifecycle
# ---------------------------------------------------------------------------


async def test_configured_false_without_uri_or_driver() -> None:
    client = Neo4jClient(Neo4jClientConfig())
    assert client.configured is False


async def test_configured_true_with_injected_driver() -> None:
    assert make_client(FakeDriver()).configured is True


async def test_configured_true_with_uri_only() -> None:
    assert Neo4jClient(Neo4jClientConfig(uri="bolt://x:7687")).configured is True


async def test_run_read_uses_configured_physical_database() -> None:
    driver = FakeDriver(records=[])
    client = Neo4jClient(
        Neo4jClientConfig(uri="bolt://fake:7687", database="neo4j", driver=driver)
    )
    await client.run_read(SCOPED_CYPHER, db_tag="sop")
    assert driver.session_databases == ["neo4j"]


async def test_close_is_idempotent() -> None:
    driver = FakeDriver()
    client = make_client(driver)
    await client.close()
    await client.close()
    assert driver.close_count == 1


async def test_async_context_manager_closes_driver() -> None:
    driver = FakeDriver()
    async with make_client(driver) as client:
        assert client.configured is True
    assert driver.close_count == 1


async def test_verify_connectivity_delegates_to_driver() -> None:
    driver = FakeDriver()
    await make_client(driver).verify_connectivity()
    driver_fail = FakeDriver(connectivity_exc=OSError("boom"))
    with pytest.raises(OSError, match="boom"):
        await make_client(driver_fail).verify_connectivity()


# ---------------------------------------------------------------------------
# Structured query logging
# ---------------------------------------------------------------------------


async def test_run_read_logs_query_and_result(caplog: pytest.LogCaptureFixture) -> None:
    driver = FakeDriver(records=[{"a": 1}, {"a": 2}])
    client = make_client(driver)
    with caplog.at_level(logging.DEBUG, logger="app.common.neo4j"):
        await client.run_read(SCOPED_CYPHER, db_tag="sop", query_name="probe_query")
    events = {r.message: r for r in caplog.records}
    assert "neo4j_query" in events
    assert "neo4j_result" in events
    query_extra = events["neo4j_query"]
    assert query_extra.query_name == "probe_query"
    assert query_extra.db_tag == "sop"
    assert query_extra.cross_db is False
    result_extra = events["neo4j_result"]
    assert result_extra.record_count == 2
    assert result_extra.db_tag == "sop"
    assert result_extra.elapsed_ms >= 0


async def test_run_read_omits_params_by_default(caplog: pytest.LogCaptureFixture) -> None:
    driver = FakeDriver(records=[])
    client = make_client(driver)
    with caplog.at_level(logging.DEBUG, logger="app.common.neo4j"):
        await client.run_read(SCOPED_CYPHER, {"needle": "R1"}, db_tag="sop")
    query_records = [r for r in caplog.records if r.message == "neo4j_query"]
    assert query_records
    assert not hasattr(query_records[0], "params")


async def test_run_read_logs_params_when_enabled(caplog: pytest.LogCaptureFixture) -> None:
    driver = FakeDriver(records=[])
    client = Neo4jClient(
        Neo4jClientConfig(uri="bolt://fake:7687", driver=driver, log_params=True)
    )
    with caplog.at_level(logging.DEBUG, logger="app.common.neo4j"):
        await client.run_read(SCOPED_CYPHER, {"needle": "R1"}, db_tag="sop")
    query_records = [r for r in caplog.records if r.message == "neo4j_query"]
    assert query_records[0].params == {"needle": "R1", "database": "sop"}


async def test_run_read_logs_error(caplog: pytest.LogCaptureFixture) -> None:
    driver = FakeDriver(run_exc=OSError("connection lost"))
    client = make_client(driver)
    with caplog.at_level(logging.DEBUG, logger="app.common.neo4j"), pytest.raises(
        OSError, match="connection lost"
    ):
        await client.run_read(SCOPED_CYPHER, db_tag="sop", query_name="boom")
    errors = [r for r in caplog.records if r.message == "neo4j_error"]
    assert errors
    assert errors[0].levelno == logging.WARNING
    assert errors[0].query_name == "boom"
    assert errors[0].error_type == "OSError"
