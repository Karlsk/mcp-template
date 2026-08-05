"""Shared test fixtures and helpers."""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator, Callable, Iterator
from contextlib import AbstractAsyncContextManager
from typing import Any

import httpx
import pytest
from mcp import ClientSession
from mcp.shared.memory import create_connected_server_and_client_session
from pydantic import SecretStr

from app.sdn.client import SDNClient
from app.server import GraphClientFactory, build_server
from app.settings import Neo4jSettings, RetrySettings, SdnSettings, Settings


@pytest.fixture(autouse=True)
def _isolate_settings_from_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep Settings deterministic: ignore the developer's .env and shell env.

    YAML loading (SDN_CONFIG_FILE) and explicit per-test monkeypatch.setenv still
    work; only the dotenv source and pre-set SDN_CONTROLLER_* env are neutralized.
    """
    Settings.model_config["env_file"] = None
    for var in (
        "SDN_CONTROLLER_BASE_URL",
        "SDN_CONTROLLER_USERNAME",
        "SDN_CONTROLLER_PASSWORD",
        "SDN_CONTROLLER_TOKEN",
        "NEO4J_URI",
        "NEO4J_USERNAME",
        "NEO4J_PASSWORD",
    ):
        monkeypatch.delenv(var, raising=False)


@pytest.fixture(autouse=True)
def _isolate_app_logger() -> Iterator[None]:
    """Snapshot/restore the ``app`` logger around every test.

    ``server.main()`` calls ``setup_logging``, which mutates the global ``app``
    logger (sets its level, disables propagation, installs a handler). Without
    isolation that state would leak across tests and break ``caplog`` elsewhere.
    """
    logger = logging.getLogger("app")
    saved_level = logger.level
    saved_propagate = logger.propagate
    saved_handlers = list(logger.handlers)
    try:
        yield
    finally:
        logger.level = saved_level
        logger.propagate = saved_propagate
        logger.handlers[:] = saved_handlers


def make_sdn_settings(base_url: str = "") -> SdnSettings:
    return SdnSettings(
        base_url=base_url,
        auth_type="bearer",
        timeout=1.0,
        retry=RetrySettings(max_retries=0, base_delay=0.0, max_delay=0.0),
        endpoints={"health": "/"},
    )


def make_graph_settings(uri: str = "") -> Neo4jSettings:
    return Neo4jSettings(uri=uri, query_timeout=1.0, max_transaction_retry_time=0.0)


# ---------------------------------------------------------------------------
# Fake Neo4j driver (AsyncDriver / AsyncSession / AsyncManagedTransaction trio)
# ---------------------------------------------------------------------------


class FakeNeo4jRecord:
    def __init__(self, data: dict[str, Any]) -> None:
        self._data = data

    def data(self) -> dict[str, Any]:
        return self._data


class FakeNeo4jResult:
    def __init__(self, records: list[dict[str, Any]]) -> None:
        self._records = [FakeNeo4jRecord(r) for r in records]

    def __aiter__(self) -> AsyncIterator[FakeNeo4jRecord]:
        self._iter = iter(self._records)
        return self

    async def __anext__(self) -> FakeNeo4jRecord:
        try:
            return next(self._iter)
        except StopIteration:
            raise StopAsyncIteration from None


class FakeNeo4jTransaction:
    def __init__(self, driver: FakeNeo4jDriver) -> None:
        self._driver = driver

    async def run(
        self, cypher: str, params: dict[str, Any], timeout: float | None = None
    ) -> FakeNeo4jResult:
        self._driver.calls.append({"cypher": cypher, "params": params, "timeout": timeout})
        if self._driver.run_exc is not None:
            raise self._driver.run_exc
        return FakeNeo4jResult(self._driver.records)


class FakeNeo4jSession:
    def __init__(self, driver: FakeNeo4jDriver, database: str | None) -> None:
        self._driver = driver
        self.database = database

    async def __aenter__(self) -> FakeNeo4jSession:
        return self

    async def __aexit__(self, *exc_info: object) -> None:
        self._driver.sessions_closed += 1

    async def execute_read(
        self, work: Any, *args: object, **kwargs: object
    ) -> list[dict[str, Any]]:
        return await work(FakeNeo4jTransaction(self._driver), *args, **kwargs)


class FakeNeo4jDriver:
    """Records ``(cypher, params, database)`` calls and returns preset records."""

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

    def session(self, database: str | None = None) -> FakeNeo4jSession:
        self.session_databases.append(database)
        return FakeNeo4jSession(self, database)

    async def verify_connectivity(self) -> None:
        if self.connectivity_exc is not None:
            raise self.connectivity_exc

    async def close(self) -> None:
        self.close_count += 1


@pytest.fixture
def unconfigured_settings() -> Settings:
    return Settings(sdn=make_sdn_settings(""), sdn_controller_token=SecretStr("tok"))


@pytest.fixture
def configured_settings() -> Settings:
    sdn = make_sdn_settings("https://sdn.example")
    return Settings(sdn=sdn, sdn_controller_token=SecretStr("tok"))


def status_transport(status: int) -> httpx.MockTransport:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(status, request=request)

    return httpx.MockTransport(handler)


@pytest.fixture
def make_session() -> Callable[..., AbstractAsyncContextManager[ClientSession]]:
    """Factory building an in-memory MCP session backed by a mock SDN controller.

    Usage::

        async with make_session(configured_settings, sdn_status=200) as session:
            await session.initialize()
            ...

    ``graph_client_factory`` injects the SOP graph client (defaults to the real
    skeleton-mode GraphClient).
    """

    def _factory(
        settings: Settings,
        *,
        sdn_status: int = 200,
        sdn_exc: type[Exception] | None = None,
        graph_client_factory: GraphClientFactory | None = None,
    ) -> AbstractAsyncContextManager[ClientSession]:
        def handler(request: httpx.Request) -> httpx.Response:
            if sdn_exc is not None:
                raise sdn_exc("boom")
            return httpx.Response(sdn_status, request=request)

        transport = httpx.MockTransport(handler)
        mcp = build_server(
            settings,
            sdn_client_factory=lambda: SDNClient(settings, transport=transport),
            graph_client_factory=graph_client_factory,
        )
        return create_connected_server_and_client_session(mcp)

    return _factory
