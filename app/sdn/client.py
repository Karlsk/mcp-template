"""SDN controller client built on the generic :class:`HttpClient`.

Translates HTTP and transport errors into the :mod:`app.sdn.exceptions` hierarchy
with SAFE public messages, so MCP tools can surface clean status to the AI agent
without leaking URLs, status codes, or credentials.

Three authentication strategies, selected by ``settings.sdn.auth_type``:

- ``no-auth``: no authentication.
- ``bearer``: a fixed API key (``SDN_CONTROLLER_TOKEN``), set once at construction.
- ``basic``: obtain a bearer token by logging in with username/password (POST
  JSON ``{username, password, device_id}`` to ``endpoints['login']``), use it
  for data requests, and refresh it automatically on HTTP 401.

Business methods (``health`` and future helpers) call :meth:`SDNClient._send` and
never touch tokens or headers — the whole token lifecycle (acquisition, caching,
refresh) is handled here and stays invisible to business code.

The skeleton implements only a ``health`` probe. Real GET methods
(``get_devices`` etc.) are added here later — see the ``# TODO(sdn-wiring)``
markers.
"""

from __future__ import annotations

import asyncio
import time
import uuid
from typing import TYPE_CHECKING, Any

import httpx
from pydantic import ValidationError

from app.common.http import AuthConfig, HttpClient, HttpClientConfig, RetryConfig
from app.sdn.exceptions import (
    SDNAuthError,
    SDNConfigError,
    SDNConnectionError,
    SDNError,
    SDNHTTPError,
    SDNNotFoundError,
)
from app.sdn.models import SDNAlertsResponse, SDNHealthResponse

if TYPE_CHECKING:
    from app.settings import RetrySettings, Settings

# Reuse a recent login failure for this many seconds before retrying the login
# endpoint, so a down or credential-rejecting controller is not hammered by
# every concurrent/serial request.
LOGIN_FAILURE_COOLDOWN = 5.0


class SDNClient:
    """Async client for an SDN controller REST API."""

    def __init__(
        self,
        settings: Settings,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
        login_transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._settings = settings
        sdn = settings.sdn
        base_url = settings.sdn_base_url
        self._auth_mode = sdn.auth_type  # "no-auth" | "bearer" | "basic"
        # Per-process device id sent on every login (some controllers require it).
        self._device_id = str(uuid.uuid4())
        self._login_http: HttpClient | None = None  # basic mode only

        # basic mode wires the login/refresh clients only when a controller is
        # actually configured — so the server still boots in skeleton mode
        # (empty base_url) without credentials.
        if self._auth_mode == "basic" and settings.sdn_is_configured():
            self._require_basic_credentials(settings)
            # Data requests carry the login-obtained bearer token; it is empty
            # until initialize()/_refresh_token populates it.
            self._http = HttpClient(
                HttpClientConfig(
                    base_url=base_url,
                    timeout=sdn.timeout,
                    auth=AuthConfig(auth_type="bearer", bearer_token=""),
                    retry=_retry_from_settings(sdn.retry),
                    ssl_verify=sdn.ssl_verify,
                    transport=transport,
                    log_bodies=sdn.http_log_bodies,
                )
            )
            # Separate no-auth client for the login endpoint. Credentials travel
            # in the JSON body (not a header), and this client never carries a
            # bearer token or routes through _send, so a login 401 cannot recurse
            # back into the refresh flow.
            self._login_http = HttpClient(
                HttpClientConfig(
                    base_url=base_url,
                    timeout=sdn.timeout,
                    auth=AuthConfig(auth_type="no-auth"),
                    retry=_retry_from_settings(sdn.retry),
                    ssl_verify=sdn.ssl_verify,
                    transport=login_transport,
                    log_bodies=sdn.http_log_bodies,
                    # Mask the freshly-issued login token in opt-in success-body
                    # logs even when token_field is e.g. "sessionId"/"jwt".
                    extra_sensitive_keys=(sdn.token_field,),
                )
            )
        else:
            # no-auth, bearer (fixed api-key), or basic-but-unconfigured: static
            # auth, no refresh. When unconfigured, business methods raise before
            # any request is sent.
            token = (
                settings.sdn_controller_token.get_secret_value()
                if settings.sdn_controller_token
                else ""
            )
            password = (
                settings.sdn_controller_password.get_secret_value()
                if settings.sdn_controller_password
                else ""
            )
            self._http = HttpClient(
                HttpClientConfig(
                    base_url=base_url,
                    timeout=sdn.timeout,
                    auth=AuthConfig(
                        auth_type=sdn.auth_type,
                        username=settings.sdn_controller_username or "",
                        password=password,
                        bearer_token=token,
                    ),
                    retry=_retry_from_settings(sdn.retry),
                    ssl_verify=sdn.ssl_verify,
                    transport=transport,
                    log_bodies=sdn.http_log_bodies,
                )
            )
            # _login_http stays None (no-auth/bearer/unconfigured need no login).

        # Refresh state (basic only; unused for no-auth/bearer).
        self._token_lock = asyncio.Lock()
        self._refresh_gen = 0  # incremented after every successful token refresh
        self._login_failed_at: float | None = None
        self._login_failure: Exception | None = None

    @property
    def configured(self) -> bool:
        """True when an SDN controller base URL is configured."""
        return self._settings.sdn_is_configured()

    def _require_configured(self) -> None:
        if not self.configured:
            raise SDNConfigError("SDN controller is not configured.")

    @staticmethod
    def _require_basic_credentials(settings: Settings) -> None:
        """basic mode requires username, password, and a login endpoint — else fail fast."""
        sdn = settings.sdn
        missing: list[str] = []
        if not settings.sdn_controller_username:
            missing.append("SDN_CONTROLLER_USERNAME")
        if not settings.sdn_controller_password:
            missing.append("SDN_CONTROLLER_PASSWORD")
        if not sdn.endpoints.get("login"):
            missing.append("sdn.endpoints.login")
        if missing:
            raise SDNConfigError(
                "auth_type=basic requires the following, which are missing: "
                + ", ".join(missing)
                + "."
            )

    async def initialize(self) -> None:
        """Fetch the first token after construction.

        basic mode logs in here (fail-fast at startup if the controller is
        unreachable or rejects credentials). no-auth/bearer are no-ops — their
        auth is fully configured at construction. The MCP lifespan calls this
        once before serving any request.
        """
        if self._auth_mode == "basic" and self.configured:
            # _refresh_gen starts at 0; passing it forces the first login (no
            # prior refresh could have advanced it).
            await self._refresh_token(self._refresh_gen)

    async def get_token(self) -> str:
        """Obtain a fresh bearer token via the login endpoint.

        Per-controller hook: override for a non-standard login contract (HTTP
        method, request body, or token JSON path). Default: POST JSON
        ``{username, password, device_id}`` (no auth header — credentials travel
        in the body) to ``endpoints['login']`` and read ``token_field`` from the
        JSON response.

        Uses the separate ``_login_http`` client so a login 401 cannot recurse
        into the refresh flow.
        """
        assert self._login_http is not None  # basic mode only; callers guarantee this
        endpoint = self._settings.sdn.endpoints["login"]
        password = (
            self._settings.sdn_controller_password.get_secret_value()
            if self._settings.sdn_controller_password
            else ""
        )
        try:
            resp = await self._login_http.post(
                endpoint,
                json={
                    "username": self._settings.sdn_controller_username or "",
                    "password": password,
                    "device_id": self._device_id,
                },
                headers={"Accept": "application/json"},
            )
        except httpx.HTTPError as exc:
            raise _map_http_error(exc) from exc
        try:
            return str(resp.json()[self._settings.sdn.token_field])
        except (ValueError, KeyError, TypeError) as exc:
            raise SDNAuthError("SDN login response was malformed.", detail=str(exc)) from exc

    async def _refresh_token(self, stale_gen: int) -> None:
        """Refresh the bearer token under a lock (one login per expiry burst).

        - Generation-based dedup: each caller captures ``stale_gen`` before its
          request and skips if the generation has advanced — i.e. another caller
          already refreshed. This collapses concurrent 401s to a single login
          even when the refreshed token string equals the expired one (which a
          value-equality check could not detect).
        - Negative cache: a login failure is reused for
          ``LOGIN_FAILURE_COOLDOWN`` seconds so concurrent and serial requests
          do not each re-attempt a (failing) login.
        """
        async with self._token_lock:
            if self._refresh_gen != stale_gen:
                return  # another coroutine already refreshed; reuse its token
            if (
                self._login_failed_at is not None
                and time.monotonic() - self._login_failed_at < LOGIN_FAILURE_COOLDOWN
                and self._login_failure is not None
            ):
                raise self._login_failure
            try:
                new_token = await self.get_token()
            except Exception as exc:
                self._login_failed_at = time.monotonic()
                self._login_failure = exc
                raise
            self._login_failed_at = None
            self._login_failure = None
            self._refresh_gen += 1
            await self._http.set_bearer_token(new_token)

    async def _send(self, method: str, url: str, **kwargs: Any) -> httpx.Response:
        """Refresh-aware request. All SDN methods must route through here.

        For no-auth/bearer this is a zero-overhead passthrough (static auth).
        For basic it refreshes the token on HTTP 401 and retries once; a second
        401 propagates so refresh can never loop.
        """
        request = getattr(self._http, method.lower())
        if self._auth_mode != "basic":
            return await request(url, **kwargs)
        stale_gen = self._refresh_gen
        try:
            return await request(url, **kwargs)
        except httpx.HTTPStatusError as exc:
            if exc.response.status_code != 401:
                raise
        # 401: refresh once, then retry once. Any further failure propagates.
        await self._refresh_token(stale_gen)
        return await request(url, **kwargs)

    async def request(self, method: str, endpoint: str, **kwargs: Any) -> httpx.Response:
        """Send a refresh-aware request to a controller endpoint.

        This is the business-call primitive: token acquisition and refresh are
        handled transparently by :meth:`_send`. Business methods (and live
        tests) route here and never touch tokens or headers. Raises a sanitized
        :class:`SDNError` on failure; never a raw httpx error.
        """
        self._require_configured()
        try:
            return await self._send(method, endpoint, **kwargs)
        except httpx.HTTPError as exc:
            raise _map_http_error(exc) from exc

    async def health(self) -> SDNHealthResponse:
        """Probe the controller's health endpoint.

        Raises a typed :class:`SDNError` subclass on failure; never a raw httpx
        error. Routes through :meth:`_send` so basic mode refreshes its token
        transparently if the probe hits a 401.
        """
        self._require_configured()
        endpoint = self._settings.sdn.endpoints.get("health", "/")
        start = time.perf_counter()
        try:
            resp = await self._send("get", endpoint)
        except httpx.HTTPError as exc:
            raise _map_http_error(exc) from exc
        latency_ms = round((time.perf_counter() - start) * 1000, 2)
        return SDNHealthResponse(
            ok=resp.status_code < 400,
            status_code=resp.status_code,
            latency_ms=latency_ms,
        )

    async def query_alerts(
        self,
        *,
        interval: str = "1h",
        namespace: str = "device",
        category: str = "PE端口Down",
        page_num: int = 1,
        page_size: int = 10,
    ) -> SDNAlertsResponse:
        """Query paged device alerts (business method).

        Owns the alert endpoint, request-body shape, and response parsing —
        business logic that must NOT live in the MCP tool layer. Token
        acquisition/refresh and HTTP-error mapping are handled transparently by
        :meth:`request`. The response is boundary-validated into a typed model.
        """
        endpoint = self._settings.sdn.endpoints.get("alerts", "/monitor/v2/alert/page")
        body = {
            "interval": interval,
            "namespace": namespace,
            "category": category,
            "pageNum": page_num,
            "pageSize": page_size,
        }
        resp = await self.request("POST", endpoint, json=body)
        try:
            return SDNAlertsResponse.model_validate(resp.json())
        except (ValueError, ValidationError) as exc:
            raise SDNError("SDN alerts response was malformed.", detail=str(exc)) from exc

    # TODO(sdn-wiring): add further business methods (get_devices, get_topology, ...):
    # async def get_devices(self) -> list[Device]:
    #     self._require_configured()
    #     try:
    #         resp = await self._send("get", self._settings.sdn.endpoints["devices"])
    #     except httpx.HTTPError as exc:
    #         raise _map_http_error(exc) from exc
    #     return [Device.model_validate(d) for d in resp.json().get("devices", [])]

    async def aclose(self) -> None:
        """Release the underlying HTTP clients. Idempotent."""
        await self._http.close()
        if self._login_http is not None:
            await self._login_http.close()


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
