"""Shared test fixtures and helpers."""

from __future__ import annotations

import logging
from collections.abc import Callable, Iterator
from contextlib import AbstractAsyncContextManager

import httpx
import pytest
from mcp import ClientSession
from mcp.shared.memory import create_connected_server_and_client_session
from pydantic import SecretStr

from app.sdn.client import SDNClient
from app.server import build_server
from app.settings import RetrySettings, SdnSettings, Settings


@pytest.fixture(autouse=True)
def _isolate_settings_from_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep Settings deterministic: ignore the developer's .env and shell env.

    YAML loading (SDN_CONFIG_FILE) and explicit per-test monkeypatch.setenv still
    work; only the dotenv source and pre-set SDN_CONTROLLER_* env are neutralized.
    """
    Settings.model_config["env_file"] = None
    for var in (
        "SDN_CONTROLLER_BASE_URL",
        "SDN_CONTROLLER_USERNAME",
        "SDN_CONTROLLER_PASSWORD",
        "SDN_CONTROLLER_TOKEN",
    ):
        monkeypatch.delenv(var, raising=False)


@pytest.fixture(autouse=True)
def _isolate_app_logger() -> Iterator[None]:
    """Snapshot/restore the ``app`` logger around every test.

    ``server.main()`` calls ``setup_logging``, which mutates the global ``app``
    logger (sets its level, disables propagation, installs a handler). Without
    isolation that state would leak across tests and break ``caplog`` elsewhere.
    """
    logger = logging.getLogger("app")
    saved_level = logger.level
    saved_propagate = logger.propagate
    saved_handlers = list(logger.handlers)
    try:
        yield
    finally:
        logger.level = saved_level
        logger.propagate = saved_propagate
        logger.handlers[:] = saved_handlers


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
    return Settings(sdn=make_sdn_settings(""), sdn_controller_token=SecretStr("tok"))


@pytest.fixture
def configured_settings() -> Settings:
    sdn = make_sdn_settings("https://sdn.example")
    return Settings(sdn=sdn, sdn_controller_token=SecretStr("tok"))


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
