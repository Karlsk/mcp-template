"""SOP graph business layer over the generic Neo4j client.

Owns endpoint concerns the generic driver must not know: the ``GraphError``
mapping (the ONLY place driver exceptions are handled), skeleton-mode guards,
and — from spec-03 on — the SOP query methods. Business methods follow one
uniform shape::

    async def some_query(self, ...) -> SomeModel:
        self._require_configured()
        try:
            rows = await self._neo4j.run_read(CYPHER_X, {...}, db_tag=db,
                                              query_name="some_query")
        except Exception as exc:                      # driver exceptions only here
            raise _map_neo4j_error(exc) from exc
        ...

``except Exception`` (rather than ``neo4j.exceptions.Neo4jError``) is deliberate:
the driver also raises ``asyncio.TimeoutError`` / ``OSError``, and every one of
them must be mapped onto a sanitized message.
"""

from __future__ import annotations

import logging

import neo4j
from neo4j import AsyncDriver

from app.common.neo4j import Neo4jClient, Neo4jClientConfig
from app.graph.exceptions import (
    GraphAuthError,
    GraphConfigError,
    GraphConnectionError,
    GraphError,
    GraphQueryError,
)
from app.settings import Settings

logger = logging.getLogger(__name__)


def _map_neo4j_error(exc: Exception) -> GraphError:
    """Map a driver exception onto the sanitized graph error hierarchy.

    Order matters: ``AuthError`` is a ``ClientError`` subclass and must be
    checked first; ``Neo4jError`` (the base) is covered by the fallback.
    """
    if isinstance(exc, neo4j.exceptions.AuthError):
        return GraphAuthError("SOP graph authentication failed.", detail=str(exc))
    if isinstance(
        exc, neo4j.exceptions.ServiceUnavailable | neo4j.exceptions.SessionExpired
    ):
        return GraphConnectionError("SOP graph is unreachable.", detail=str(exc))
    if isinstance(exc, neo4j.exceptions.ClientError):
        return GraphQueryError("SOP graph rejected the query.", detail=str(exc))
    return GraphError("SOP graph request failed.", detail=str(exc))


class GraphClient:
    """SOP graph business methods over the generic Neo4j client."""

    def __init__(self, settings: Settings, *, driver: AsyncDriver | None = None) -> None:
        self._settings = settings
        self._neo4j = Neo4jClient(
            Neo4jClientConfig(
                uri=settings.neo4j_base_uri,
                username=settings.neo4j_username or "",
                password=(
                    settings.neo4j_password.get_secret_value()
                    if settings.neo4j_password
                    else ""
                ),
                database=settings.neo4j.database,
                query_timeout=settings.neo4j.query_timeout,
                max_transaction_retry_time=settings.neo4j.max_transaction_retry_time,
                log_params=settings.neo4j.log_params,
                driver=driver,
            )
        )

    @property
    def configured(self) -> bool:
        return self._neo4j.configured

    def _require_configured(self) -> None:
        if not self.configured:
            raise GraphConfigError("SOP graph is not configured.")

    async def probe(self) -> bool:
        """Best-effort connectivity check at startup. Never raises.

        Unlike the SDN basic-auth login this is NOT fail-fast: SDN tools must
        keep working when the SOP graph is down (spec-02 decision 7).
        """
        if not self.configured:
            return False
        try:
            await self._neo4j.verify_connectivity()
        except Exception as exc:
            logger.warning(
                "graph_probe_failed", extra={"error_type": type(exc).__name__}
            )
            return False
        return True

    async def aclose(self) -> None:
        """Close the underlying driver (idempotent)."""
        await self._neo4j.close()
