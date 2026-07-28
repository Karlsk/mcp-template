"""SDN controller client built on the generic :class:`HttpClient`.

Translates HTTP and transport errors into the :mod:`app.sdn.exceptions` hierarchy
with SAFE public messages, so MCP tools can surface clean status to the AI agent
without leaking URLs, status codes, or credentials.

The skeleton implements only a ``health`` probe. Real GET methods (``get_devices``
etc.) are added here later — see the ``# TODO(sdn-wiring)`` markers.
"""

from __future__ import annotations

import time
from typing import TYPE_CHECKING

import httpx

from app.common.http import AuthConfig, HttpClient, HttpClientConfig, RetryConfig
from app.sdn.exceptions import (
    SDNAuthError,
    SDNConfigError,
    SDNConnectionError,
    SDNError,
    SDNHTTPError,
    SDNNotFoundError,
)
from app.sdn.models import SDNHealthResponse

if TYPE_CHECKING:
    from app.settings import RetrySettings, Settings


class SDNClient:
    """Async client for an SDN controller REST API."""

    def __init__(
        self, settings: Settings, *, transport: httpx.AsyncBaseTransport | None = None
    ) -> None:
        self._settings = settings
        sdn = settings.sdn
        token = settings.sdn_token.get_secret_value() if settings.sdn_token else ""
        password = settings.sdn_password.get_secret_value() if settings.sdn_password else ""
        auth = AuthConfig(
            auth_type=sdn.auth_type,
            username=settings.sdn_username or "",
            password=password,
            bearer_token=token,
        )
        self._http = HttpClient(
            HttpClientConfig(
                base_url=sdn.base_url,
                timeout=sdn.timeout,
                auth=auth,
                retry=_retry_from_settings(sdn.retry),
                transport=transport,
            )
        )

    @property
    def configured(self) -> bool:
        """True when an SDN controller base URL is configured."""
        return self._settings.sdn_is_configured()

    def _require_configured(self) -> None:
        if not self.configured:
            raise SDNConfigError("SDN controller is not configured.")

    async def health(self) -> SDNHealthResponse:
        """Probe the controller's health endpoint.

        Raises a typed :class:`SDNError` subclass on failure; never a raw httpx
        error. Use ``raise_for_status=True`` so 4xx/5xx surface as SDNErrors.
        """
        self._require_configured()
        endpoint = self._settings.sdn.endpoints.get("health", "/")
        start = time.perf_counter()
        try:
            resp = await self._http.get(endpoint)
        except httpx.HTTPError as exc:
            raise _map_http_error(exc) from exc
        latency_ms = round((time.perf_counter() - start) * 1000, 2)
        return SDNHealthResponse(
            ok=resp.status_code < 400,
            status_code=resp.status_code,
            latency_ms=latency_ms,
        )

    # TODO(sdn-wiring): add real GET methods, e.g.:
    # async def get_devices(self) -> list[Device]:
    #     self._require_configured()
    #     try:
    #         resp = await self._http.get(self._settings.sdn.endpoints["devices"])
    #     except httpx.HTTPError as exc:
    #         raise _map_http_error(exc) from exc
    #     return [Device.model_validate(d) for d in resp.json().get("devices", [])]

    async def aclose(self) -> None:
        """Release the underlying HTTP client. Idempotent."""
        await self._http.close()


def _retry_from_settings(retry: RetrySettings) -> RetryConfig:
    return RetryConfig(
        max_retries=retry.max_retries,
        base_delay=retry.base_delay,
        max_delay=retry.max_delay,
    )


def _map_http_error(exc: Exception) -> SDNError:
    """Translate an httpx exception into a SAFE :class:`SDNError`.

    Public messages are human-authored and contain no URLs, status-derived
    paths, or credentials. The raw exception is preserved as ``detail`` for
    server-side logging only.
    """
    if isinstance(exc, httpx.ConnectError):
        return SDNConnectionError("Cannot connect to the SDN controller.", detail=str(exc))
    if isinstance(exc, httpx.TimeoutException):
        return SDNConnectionError("Timed out contacting the SDN controller.", detail=str(exc))
    if isinstance(exc, httpx.TransportError):
        return SDNConnectionError("Network error contacting the SDN controller.", detail=str(exc))
    if isinstance(exc, httpx.HTTPStatusError):
        code = exc.response.status_code
        if code in (401, 403):
            return SDNAuthError("SDN controller rejected credentials.", detail=str(exc))
        if code == 404:
            return SDNNotFoundError("SDN controller resource not found.", detail=str(exc))
        return SDNHTTPError("SDN controller returned an error response.", detail=str(exc))
    return SDNError("Unexpected error contacting the SDN controller.", detail=str(exc))
