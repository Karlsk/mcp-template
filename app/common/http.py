"""Async HTTP client with retry, authentication, and REST method wrappers.

This is a generic, integration-agnostic client. SDN-specific concerns (endpoint
mapping, response models, error sanitization) live in ``app/sdn``.

Design notes:
- Retries only on *retriable* conditions: transport errors and HTTP 429/5xx.
  Client errors (4xx except 429) are raised immediately — retrying them is a bug.
- Retry delay uses *full jitter* (random within ``[0, cap]``) so concurrent
  clients don't retry in lockstep and amplify load on a shared controller.
- Accepts an injectable ``transport`` (e.g. ``httpx.MockTransport``) so the retry
  and auth logic can be unit-tested without a real server.
- Usable as an async context manager (``async with HttpClient(...) as c:``) for
  deterministic resource cleanup.

Security note: each attempt emits structured ``http_request`` / ``http_response``
/ ``http_error`` log records carrying the URL and status as fields. These are
server-side only. The token always lives in the ``Authorization`` header (never
the URL) and headers are never logged. On HTTP errors a redacted, truncated
response-body excerpt is logged at WARNING so the controller's error is visible;
success-path request/response bodies are logged at DEBUG only when
``HttpClientConfig.log_bodies`` is set, with sensitive keys (password / token /
authorization / ...) masked. Do not forward these logs to a channel the model can
read — sanitize at the SDN layer (see ``app/sdn``).
"""

from __future__ import annotations

import asyncio
import json
import logging
import random
import time
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any, Final

import httpx

logger = logging.getLogger(__name__)

Headers = Mapping[str, str]

# Keys whose values are masked in logged request/response bodies. Matched as a
# case-insensitive substring of the JSON key (so "access_token", "api_key", and
# "user_password" are all caught). Per-client additions come from
# HttpClientConfig.extra_sensitive_keys (e.g. the login-response token field).
_SENSITIVE: Final[tuple[str, ...]] = (
    "password", "secret", "token", "authorization", "api_key", "apikey", "cookie",
)
_REDACTED = "***"


def _is_sensitive(key: str, extra: tuple[str, ...]) -> bool:
    lowered = key.lower()
    return any(s in lowered for s in _SENSITIVE) or any(s.lower() in lowered for s in extra)


def _redact(obj: Any, extra_sensitive: tuple[str, ...]) -> Any:
    """Recursively mask values of sensitive keys in a JSON-like structure."""
    if isinstance(obj, dict):
        return {
            key: (
                _REDACTED
                if _is_sensitive(str(key), extra_sensitive)
                else _redact(value, extra_sensitive)
            )
            for key, value in obj.items()
        }
    if isinstance(obj, list):
        return [_redact(item, extra_sensitive) for item in obj]
    return obj


def _truncate_text(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    return f"{text[:limit]}...(truncated, {len(text)} bytes)"


def _redact_request_body(
    json_body: Any,
    params: dict[str, Any] | None,
    content: str | bytes | None,
    limit: int,
    extra_sensitive: tuple[str, ...],
) -> dict[str, Any] | None:
    """Build a redacted, truncated view of the request payload for logs."""
    parts: dict[str, Any] = {}
    if json_body is not None:
        parts["json"] = _truncate_text(
            json.dumps(_redact(json_body, extra_sensitive), ensure_ascii=False, default=str),
            limit,
        )
    if params:
        parts["params"] = _redact(params, extra_sensitive)
    if content is not None:
        text = content.decode("utf-8", errors="replace") if isinstance(content, bytes) else content
        parts["content"] = _truncate_text(text, limit)
    return parts or None


def _redact_response_body(
    response: httpx.Response, limit: int, extra_sensitive: tuple[str, ...]
) -> str | None:
    """Return a redacted, truncated string view of a response body for logs.

    Parses JSON when possible (masking sensitive keys), falls back to raw text,
    and finally to a size placeholder for undecodable (binary) bodies.
    """
    try:
        parsed = response.json()
    except ValueError:
        try:
            return _truncate_text(response.text, limit)
        except Exception:
            return f"<binary {len(response.content)} bytes>"
    redacted = _redact(parsed, extra_sensitive)
    return _truncate_text(json.dumps(redacted, ensure_ascii=False, default=str), limit)


@dataclass
class AuthConfig:
    """Authentication configuration."""

    auth_type: str = "no-auth"  # "no-auth" | "bearer" | "basic"
    username: str = ""
    password: str = ""
    bearer_token: str = ""


@dataclass
class RetryConfig:
    """Retry configuration with exponential backoff."""

    max_retries: int = 3
    base_delay: float = 1.0
    max_delay: float = 30.0


@dataclass
class HttpClientConfig:
    """HTTP client configuration."""

    base_url: str = ""
    timeout: float = 30.0
    auth: AuthConfig = field(default_factory=AuthConfig)
    retry: RetryConfig = field(default_factory=RetryConfig)
    headers: dict[str, str] = field(default_factory=dict)
    ssl_verify: bool = True
    transport: httpx.AsyncBaseTransport | None = None
    # Opt-in: log redacted, truncated request/response bodies at DEBUG (the
    # error-path response body is always logged at WARNING regardless of this).
    log_bodies: bool = False
    body_log_limit: int = 2048
    # Extra JSON keys to mask in addition to the built-in sensitive set.
    extra_sensitive_keys: tuple[str, ...] = ()


class HttpClient:
    """Async HTTP client with retry and authentication support."""

    def __init__(self, config: HttpClientConfig | None = None) -> None:
        self._config = config or HttpClientConfig()
        self._client: httpx.AsyncClient | None = None

    async def __aenter__(self) -> HttpClient:
        await self._get_client()
        return self

    async def __aexit__(self, *exc_info: object) -> None:
        await self.close()

    def _build_auth(self) -> httpx.Auth | None:
        """Build httpx auth from config (basic auth). Bearer is sent via headers."""
        if self._config.auth.auth_type == "basic" and self._config.auth.username:
            return httpx.BasicAuth(self._config.auth.username, self._config.auth.password)
        return None

    def _build_headers(self) -> dict[str, str]:
        """Build request headers including the bearer token if configured."""
        headers = dict(self._config.headers)
        if self._config.auth.auth_type == "bearer" and self._config.auth.bearer_token:
            headers["Authorization"] = f"Bearer {self._config.auth.bearer_token}"
        return headers

    @property
    def bearer_token(self) -> str:
        """The currently configured bearer token (empty when bearer auth is unused/unset)."""
        return self._config.auth.bearer_token

    async def set_bearer_token(self, token: str) -> None:
        """Rotate the bearer token on the live client without rebuilding it.

        Updates the stored config (so a later lazy rebuild preserves the new
        token) and the live client's headers. Generic primitive only — it holds
        no refresh policy; the caller decides *when* to rotate.
        """
        self._config.auth.bearer_token = token
        client = await self._get_client()
        if token:
            client.headers["Authorization"] = f"Bearer {token}"
        else:
            client.headers.pop("Authorization", None)

    @staticmethod
    def _is_retriable(exc: Exception | None) -> bool:
        """Decide whether an exception is worth retrying.

        - Transport errors (connect/timeout/read): retry.
        - HTTP 429 and 5xx: retry.
        - Any other 4xx: do NOT retry (client error, will not succeed).
        """
        if isinstance(exc, httpx.TransportError):
            return True
        if isinstance(exc, httpx.HTTPStatusError) and exc.response is not None:
            code = exc.response.status_code
            return code == 429 or code >= 500
        return False

    async def _get_client(self) -> httpx.AsyncClient:
        """Get or lazily create the underlying httpx client."""
        if self._client is None or self._client.is_closed:
            self._client = httpx.AsyncClient(
                base_url=self._config.base_url,
                timeout=self._config.timeout,
                auth=self._build_auth(),
                headers=self._build_headers(),
                verify=self._config.ssl_verify,
                transport=self._config.transport,
            )
        return self._client

    def _backoff_delay(self, attempt: int) -> float:
        """Full-jitter exponential backoff delay (seconds) for the given attempt."""
        cap = min(
            self._config.retry.base_delay * (2 ** (attempt - 1)),
            self._config.retry.max_delay,
        )
        # Full jitter (AWS "Exponential Backoff and Jitter"): randomize within
        # [0, cap] so concurrent clients don't retry in lockstep.
        return random.uniform(0, cap)

    async def _request(
        self,
        method: str,
        url: str,
        *,
        params: dict[str, Any] | None = None,
        json: Any = None,
        content: str | bytes | None = None,
        headers: Headers | None = None,
        raise_for_status: bool = True,
    ) -> httpx.Response:
        """Execute an HTTP request, retrying only on retriable failures.

        Emits structured ``http_request`` / ``http_response`` / ``http_error``
        log records per attempt (see the module docstring for the body policy).
        """
        client = await self._get_client()
        max_attempts = self._config.retry.max_retries + 1
        last_exc: Exception | None = None
        log_bodies = self._config.log_bodies
        body_limit = self._config.body_log_limit
        extra_sensitive = self._config.extra_sensitive_keys

        for attempt in range(1, max_attempts + 1):
            logger.debug(
                "http_request",
                extra={
                    "method": method,
                    "url": url,
                    "attempt": attempt,
                    "max_attempts": max_attempts,
                    "request_body": (
                        _redact_request_body(json, params, content, body_limit, extra_sensitive)
                        if log_bodies
                        else None
                    ),
                },
            )
            start = time.perf_counter()
            try:
                response = await client.request(
                    method, url, params=params, json=json, content=content, headers=headers
                )
                if raise_for_status:
                    response.raise_for_status()
                elapsed_ms = round((time.perf_counter() - start) * 1000, 2)
                logger.debug(
                    "http_response",
                    extra={
                        "method": method,
                        "url": url,
                        "attempt": attempt,
                        "status": response.status_code,
                        "elapsed_ms": elapsed_ms,
                        "response_body": (
                            _redact_response_body(response, body_limit, extra_sensitive)
                            if log_bodies
                            else None
                        ),
                    },
                )
                return response
            except Exception as exc:
                elapsed_ms = round((time.perf_counter() - start) * 1000, 2)
                last_exc = exc
                retriable = self._is_retriable(exc)
                will_retry = retriable and attempt < max_attempts
                delay = self._backoff_delay(attempt)
                status: int | None = None
                response_body: str | None = None
                if isinstance(exc, httpx.HTTPStatusError):
                    status = exc.response.status_code
                    response_body = _redact_response_body(
                        exc.response, body_limit, extra_sensitive
                    )
                logger.warning(
                    "http_error",
                    extra={
                        "method": method,
                        "url": url,
                        "attempt": attempt,
                        "max_attempts": max_attempts,
                        "status": status,
                        "elapsed_ms": elapsed_ms,
                        "error": type(exc).__name__,
                        "response_body": response_body,
                        "retry_in": delay if will_retry else None,
                    },
                )
                if not retriable:
                    raise
                if not will_retry:
                    break
                await asyncio.sleep(delay)

        assert last_exc is not None  # loop only exits early via return/raise when exhausted
        raise last_exc

    async def get(
        self, url: str, *, params: dict[str, Any] | None = None,
        headers: Headers | None = None, raise_for_status: bool = True,
    ) -> httpx.Response:
        """Send a GET request."""
        return await self._request(
            "GET", url, params=params, headers=headers, raise_for_status=raise_for_status
        )

    async def post(
        self, url: str, *, json: Any = None, params: dict[str, Any] | None = None,
        headers: Headers | None = None, raise_for_status: bool = True,
    ) -> httpx.Response:
        """Send a POST request."""
        return await self._request(
            "POST", url, params=params, json=json, headers=headers,
            raise_for_status=raise_for_status,
        )

    async def put(
        self, url: str, *, json: Any = None, content: str | bytes | None = None,
        params: dict[str, Any] | None = None, headers: Headers | None = None,
        raise_for_status: bool = True,
    ) -> httpx.Response:
        """Send a PUT request."""
        return await self._request(
            "PUT", url, params=params, json=json, content=content, headers=headers,
            raise_for_status=raise_for_status,
        )

    async def patch(
        self, url: str, *, json: Any = None, content: str | bytes | None = None,
        params: dict[str, Any] | None = None, headers: Headers | None = None,
        raise_for_status: bool = True,
    ) -> httpx.Response:
        """Send a PATCH request."""
        return await self._request(
            "PATCH", url, params=params, json=json, content=content, headers=headers,
            raise_for_status=raise_for_status,
        )

    async def delete(
        self, url: str, *, params: dict[str, Any] | None = None,
        headers: Headers | None = None, raise_for_status: bool = True,
    ) -> httpx.Response:
        """Send a DELETE request."""
        return await self._request(
            "DELETE", url, params=params, headers=headers, raise_for_status=raise_for_status
        )

    async def head(
        self, url: str, *, params: dict[str, Any] | None = None,
        headers: Headers | None = None, raise_for_status: bool = True,
    ) -> httpx.Response:
        """Send a HEAD request."""
        return await self._request(
            "HEAD", url, params=params, headers=headers, raise_for_status=raise_for_status
        )

    async def close(self) -> None:
        """Close the underlying HTTP client. Idempotent."""
        if self._client is not None and not self._client.is_closed:
            await self._client.aclose()
            self._client = None
