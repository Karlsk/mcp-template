"""Tests for the SDN client: health probe and error sanitization."""

from __future__ import annotations

import httpx
import pytest
from pydantic import SecretStr

from app.sdn.client import SDNClient
from app.sdn.exceptions import (
    SDNAuthError,
    SDNConfigError,
    SDNConnectionError,
    SDNError,
    SDNHTTPError,
    SDNNotFoundError,
)
from app.settings import RetrySettings, SdnSettings, Settings

BASE_URL = "https://sdn.example"


def make_settings(*, base_url: str = BASE_URL, **sdn_overrides: object) -> Settings:
    sdn = SdnSettings(
        base_url=base_url,
        auth_type="bearer",
        timeout=1.0,
        retry=RetrySettings(max_retries=0, base_delay=0.0, max_delay=0.0),
        endpoints={"health": "/"},
        **sdn_overrides,
    )
    return Settings(sdn=sdn, sdn_token=SecretStr("tok"))


def _status_handler(status: int) -> httpx.MockTransport:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(status, request=request)

    return httpx.MockTransport(handler)


async def test_configured_property_true() -> None:
    client = SDNClient(make_settings(), transport=_status_handler(200))
    try:
        assert client.configured is True
    finally:
        await client.aclose()


async def test_configured_property_false() -> None:
    client = SDNClient(make_settings(base_url=""), transport=_status_handler(200))
    try:
        assert client.configured is False
    finally:
        await client.aclose()


async def test_unconfigured_raises_config_error() -> None:
    client = SDNClient(make_settings(base_url=""), transport=_status_handler(200))
    try:
        with pytest.raises(SDNConfigError):
            await client.health()
    finally:
        await client.aclose()


async def test_health_success() -> None:
    client = SDNClient(make_settings(), transport=_status_handler(200))
    try:
        result = await client.health()
    finally:
        await client.aclose()
    assert result.ok is True
    assert result.status_code == 200
    assert result.latency_ms >= 0


async def test_health_auth_error_is_sanitized() -> None:
    """A 401 must surface as SDNAuthError with NO URL in the public message."""
    client = SDNClient(make_settings(), transport=_status_handler(401))
    try:
        with pytest.raises(SDNAuthError) as exc_info:
            await client.health()
    finally:
        await client.aclose()
    err = exc_info.value
    assert BASE_URL not in str(err)
    assert BASE_URL not in err.public_message
    # The raw (URL-bearing) detail is kept for server-side logs only.
    assert err.detail is not None
    assert BASE_URL in err.detail


async def test_health_not_found_error() -> None:
    client = SDNClient(make_settings(), transport=_status_handler(404))
    try:
        with pytest.raises(SDNNotFoundError):
            await client.health()
    finally:
        await client.aclose()


async def test_health_server_error() -> None:
    client = SDNClient(make_settings(), transport=_status_handler(500))
    try:
        with pytest.raises(SDNHTTPError) as exc_info:
            await client.health()
    finally:
        await client.aclose()
    assert BASE_URL not in str(exc_info.value)


async def test_health_connection_error() -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("boom")

    client = SDNClient(make_settings(), transport=httpx.MockTransport(handler))
    try:
        with pytest.raises(SDNConnectionError) as exc_info:
            await client.health()
    finally:
        await client.aclose()
    assert "Cannot connect" in str(exc_info.value)


async def test_health_timeout_error() -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("slow")

    client = SDNClient(make_settings(), transport=httpx.MockTransport(handler))
    try:
        with pytest.raises(SDNConnectionError):
            await client.health()
    finally:
        await client.aclose()


async def test_close_is_idempotent() -> None:
    client = SDNClient(make_settings(), transport=_status_handler(200))
    await client.aclose()
    await client.aclose()


def test_map_http_error_generic_transport_error() -> None:
    """A non-connect/timeout TransportError (e.g. ReadError) maps to SDNConnectionError."""
    from app.sdn.client import _map_http_error

    err = _map_http_error(httpx.ReadError("read failed"))
    assert isinstance(err, SDNConnectionError)
    assert "Network error" in str(err)


def test_map_http_error_fallback_for_unexpected_http_error() -> None:
    """A non-transport/non-status httpx.HTTPError hits the safe fallback branch."""
    from app.sdn.client import _map_http_error

    err = _map_http_error(httpx.HTTPError("weird http error http://leak/url"))
    assert isinstance(err, SDNError)
    assert not isinstance(err, SDNConnectionError | SDNAuthError | SDNNotFoundError | SDNHTTPError)
    assert "Unexpected error" in str(err)
    # The raw (possibly URL-bearing) message must stay in detail only.
    assert err.detail is not None
    assert "leak/url" in err.detail
    assert "leak/url" not in str(err)
