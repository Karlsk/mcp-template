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

Security note: the retry ``logger.warning`` logs ``str(exc)``, which for an httpx
error embeds the request URL. This is server-side only by design (the token lives
in the ``Authorization`` header, never the URL). Do not forward these logs to a
channel the model can read — sanitize at the SDN layer (see ``app/sdn``).
"""

from __future__ import annotations

import asyncio
import logging
import random
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

import httpx

logger = logging.getLogger(__name__)

Headers = Mapping[str, str]


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
        """Execute an HTTP request, retrying only on retriable failures."""
        client = await self._get_client()
        max_attempts = self._config.retry.max_retries + 1
        last_exc: Exception | None = None

        for attempt in range(1, max_attempts + 1):
            try:
                response = await client.request(
                    method, url, params=params, json=json, content=content, headers=headers
                )
                if raise_for_status:
                    response.raise_for_status()
                return response
            except Exception as exc:
                last_exc = exc
                if not self._is_retriable(exc):
                    raise
                if attempt >= max_attempts:
                    break
                cap = min(
                    self._config.retry.base_delay * (2 ** (attempt - 1)),
                    self._config.retry.max_delay,
                )
                # Full jitter (AWS "Exponential Backoff and Jitter"): randomize the
                # delay within [0, cap] so concurrent clients don't retry in lockstep.
                delay = random.uniform(0, cap)
                logger.warning(
                    "Request %s %s failed (attempt %d/%d), retrying in %.2fs: %s",
                    method, url, attempt, max_attempts, delay, exc,
                )
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
