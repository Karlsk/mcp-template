"""SOP graph error hierarchy.

Every exception carries a SAFE ``public_message`` (the only thing that may reach
the AI agent) and an optional ``detail`` (server-side context only, never exposed
via ``__str__``). This is the sanitization primitive that stops neo4j driver
error strings — which embed bolt URIs, Cypher fragments, and stacktrace
summaries — from leaking to the model.
"""

from __future__ import annotations


class GraphError(Exception):
    """Base class for all SOP graph errors."""

    def __init__(self, public_message: str, *, detail: str | None = None) -> None:
        super().__init__(public_message)
        self.public_message = public_message
        self.detail = detail

    def __str__(self) -> str:
        # Only the safe, human-authored message — never the raw detail.
        return self.public_message


class GraphConfigError(GraphError):
    """The SOP graph is not configured (missing URI or credentials)."""


class GraphConnectionError(GraphError):
    """The SOP graph could not be reached (network, timeout, or unavailable)."""


class GraphAuthError(GraphError):
    """Authentication to the SOP graph failed."""


class GraphQueryError(GraphError):
    """The graph rejected the query, or its response was malformed."""
