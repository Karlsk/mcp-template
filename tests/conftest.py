"""Shared test fixtures and helpers."""

from __future__ import annotations

from collections.abc import Callable
from contextlib import AbstractAsyncContextManager

import httpx
import pytest
from mcp import ClientSession
from mcp.shared.memory import create_connected_server_and_client_session
from pydantic import SecretStr

from app.sdn.client import SDNClient
from app.server import build_server
from app.settings import RetrySettings, SdnSettings, Settings


def make_sdn_settings(base_url: str = "") -> SdnSettings:
    return SdnSettings(
        base_url=base_url,
        auth_type="bearer",
        timeout=1.0,
        retry=RetrySettings(max_retries=0, base_delay=0.0, max_delay=0.0),
        endpoints={"health": "/"},
    )


@pytest.fixture
def unconfigured_settings() -> Settings:
    return Settings(sdn=make_sdn_settings(""), sdn_token=SecretStr("tok"))


@pytest.fixture
def configured_settings() -> Settings:
    return Settings(sdn=make_sdn_settings("https://sdn.example"), sdn_token=SecretStr("tok"))


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
    """

    def _factory(
        settings: Settings,
        *,
        sdn_status: int = 200,
        sdn_exc: type[Exception] | None = None,
    ) -> AbstractAsyncContextManager[ClientSession]:
        def handler(request: httpx.Request) -> httpx.Response:
            if sdn_exc is not None:
                raise sdn_exc("boom")
            return httpx.Response(sdn_status, request=request)

        transport = httpx.MockTransport(handler)
        mcp = build_server(
            settings,
            sdn_client_factory=lambda: SDNClient(settings, transport=transport),
        )
        return create_connected_server_and_client_session(mcp)

    return _factory
