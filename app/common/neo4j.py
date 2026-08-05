"""Async Neo4j client with managed read transactions and query logging.

Generic and integration-agnostic: it knows nothing about SOP labels, Cypher
statements, or response models — those live in ``app/graph``.

Design notes:
- Retries are delegated to the driver's managed transactions
  (``session.execute_read``), which retry transient failures within
  ``max_transaction_retry_time``. Unlike ``HttpClient`` there is no hand-rolled
  backoff loop here; re-implementing one would fight the driver.
- Logical multi-database: the community edition has a single physical database,
  so tenancy is a node property (``_db``). ``run_read`` requires the caller to
  pass ``db_tag`` on EVERY call and asserts the query filters on ``$_db``;
  crossing logical databases requires an explicit ``allow_cross_db=True``.
- Accepts an injectable ``driver`` so query construction and error mapping can be
  unit-tested without a real server (mirrors ``HttpClientConfig.transport``).

Security note: structured ``neo4j_query`` / ``neo4j_result`` / ``neo4j_error``
records are server-side only. The password is never logged; query parameters are
logged only when ``log_params`` is set (DEBUG), since they may carry device
identifiers. Do not forward these logs to a channel the model can read.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from typing import Any, Final

from neo4j import AsyncDriver, AsyncGraphDatabase, AsyncManagedTransaction

logger = logging.getLogger(__name__)

_DB_PARAM: Final[str] = "$_db"


def _check_db_scope(cypher: str, db_tag: str | None, allow_cross_db: bool) -> None:
    """Fail fast unless the query is either db-scoped or explicitly cross-db."""
    if db_tag is None:
        if not allow_cross_db:
            raise ValueError(
                "db_tag is None but allow_cross_db is False: a cross-logical-db "
                "query must be requested explicitly."
            )
        return
    if _DB_PARAM not in cypher:
        raise ValueError(
            f"cypher must filter on {_DB_PARAM} when db_tag is given "
            "(logical database isolation)."
        )


@dataclass
class Neo4jClientConfig:
    """Neo4j client configuration.

    ``database`` is the PHYSICAL Neo4j database (community edition: always
    "neo4j"). Logical databases are not configured here — see ``run_read``.
    """

    uri: str = ""
    username: str = ""
    password: str = ""
    database: str = "neo4j"
    query_timeout: float = 30.0
    max_transaction_retry_time: float = 15.0
    # Injectable driver (e.g. a fake) so query logic is unit-testable.
    driver: AsyncDriver | None = field(default=None, repr=False)
    log_params: bool = False


class Neo4jClient:
    """Async Neo4j client exposing read-only, db-scoped query execution."""

    def __init__(self, config: Neo4jClientConfig | None = None) -> None:
        self._config = config or Neo4jClientConfig()
        self._driver: AsyncDriver | None = None
        self._closed = False

    @property
    def configured(self) -> bool:
        """True when a URI is configured or a driver was injected."""
        return bool(self._config.uri) or self._config.driver is not None

    async def __aenter__(self) -> Neo4jClient:
        return self

    async def __aexit__(self, *exc_info: object) -> None:
        await self.close()

    async def _get_driver(self) -> AsyncDriver:
        """Lazily create (or return the injected) driver.

        Not thread-hostile: one client instance is owned by one MCP session.
        Lazy creation means a configured-but-down graph cannot break lifespan.
        """
        if self._config.driver is not None:
            return self._config.driver
        if self._driver is None:
            self._driver = AsyncGraphDatabase.driver(
                self._config.uri,
                auth=(self._config.username, self._config.password),
                max_transaction_retry_time=self._config.max_transaction_retry_time,
            )
        return self._driver

    async def verify_connectivity(self) -> None:
        """Driver-level connectivity check; raises on failure."""
        driver = await self._get_driver()
        await driver.verify_connectivity()

    async def run_read(
        self,
        cypher: str,
        params: dict[str, Any] | None = None,
        *,
        db_tag: str | None,
        allow_cross_db: bool = False,
        query_name: str = "query",
    ) -> list[dict[str, Any]]:
        """Run a read-only query inside a managed (driver-retried) transaction.

        ``db_tag`` scopes the query to one logical database: it is injected as
        the ``_db`` parameter and the cypher must filter on ``$_db``. Passing
        ``db_tag=None`` requires ``allow_cross_db=True`` (explicit cross-db).
        """
        _check_db_scope(cypher, db_tag, allow_cross_db)
        merged = dict(params or {})
        if db_tag is not None:
            # db_tag wins over any caller-supplied `_db`: the scope is decided
            # by the explicit argument, never by a leftover value in `params`.
            merged["_db"] = db_tag
        query_extra: dict[str, object] = {
            "query_name": query_name,
            "db_tag": db_tag,
            "cross_db": db_tag is None,
        }
        if self._config.log_params:
            query_extra["params"] = merged
        logger.debug("neo4j_query", extra=query_extra)
        started = time.monotonic()
        try:
            driver = await self._get_driver()

            async def _work(tx: AsyncManagedTransaction) -> list[dict[str, Any]]:
                result = await tx.run(
                    cypher, merged, timeout=self._config.query_timeout
                )
                return [record.data() async for record in result]

            async with driver.session(database=self._config.database) as session:
                records = await session.execute_read(_work)
        except Exception as exc:
            elapsed_ms = round((time.monotonic() - started) * 1000, 2)
            logger.warning(
                "neo4j_error",
                extra={
                    "query_name": query_name,
                    "db_tag": db_tag,
                    "error_type": type(exc).__name__,
                    "elapsed_ms": elapsed_ms,
                },
            )
            raise
        elapsed_ms = round((time.monotonic() - started) * 1000, 2)
        logger.debug(
            "neo4j_result",
            extra={
                "query_name": query_name,
                "db_tag": db_tag,
                "record_count": len(records),
                "elapsed_ms": elapsed_ms,
            },
        )
        return records

    async def close(self) -> None:
        """Close the underlying driver. Idempotent."""
        if self._closed:
            return
        self._closed = True
        if self._config.driver is not None:
            # An injected driver is owned by the test/factory; the client still
            # closes it exactly once so teardown paths stay safe.
            await self._config.driver.close()
        elif self._driver is not None:
            await self._driver.close()
            self._driver = None
