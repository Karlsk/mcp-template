"""SDN controller error hierarchy.

Every exception carries a SAFE ``public_message`` (the only thing that may reach
the AI agent) and an optional ``detail`` (server-side context only, never exposed
via ``__str__``). This is the sanitization primitive that stops httpx error
strings — which embed URLs and status codes — from leaking to the model.
"""

from __future__ import annotations


class SDNError(Exception):
    """Base class for all SDN controller errors."""

    def __init__(self, public_message: str, *, detail: str | None = None) -> None:
        super().__init__(public_message)
        self.public_message = public_message
        self.detail = detail

    def __str__(self) -> str:
        # Only the safe, human-authored message — never the raw detail.
        return self.public_message


class SDNConfigError(SDNError):
    """The SDN controller is not configured (missing base URL or credentials)."""


class SDNConnectionError(SDNError):
    """The SDN controller could not be reached (network or timeout)."""


class SDNAuthError(SDNError):
    """Authentication or authorization failed (HTTP 401/403)."""


class SDNNotFoundError(SDNError):
    """The requested SDN resource was not found (HTTP 404)."""


class SDNHTTPError(SDNError):
    """An unexpected non-success HTTP response from the SDN controller."""
