"""Tests for the CLI entrypoint (main)."""

from __future__ import annotations

import pytest

from app import server
from app.settings import Settings


class _FakeMCP:
    """Async runner stubs: main() drives the server via ``_run_process`` now."""

    def __init__(self, recorder: dict[str, object]) -> None:
        self._recorder = recorder

    async def run_streamable_http_async(self) -> None:
        self._recorder["transport"] = "streamable-http"

    async def run_stdio_async(self) -> None:
        self._recorder["transport"] = "stdio"


def test_main_default_transport_is_streamable_http(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    recorder: dict[str, object] = {}
    monkeypatch.setattr(server, "build_server", lambda *a, **kw: _FakeMCP(recorder))
    assert server.main([]) == 0
    assert recorder["transport"] == "streamable-http"


def test_main_stdio_transport(monkeypatch: pytest.MonkeyPatch) -> None:
    recorder: dict[str, object] = {}
    monkeypatch.setattr(server, "build_server", lambda *a, **kw: _FakeMCP(recorder))
    assert server.main(["--transport", "stdio"]) == 0
    assert recorder["transport"] == "stdio"


def test_main_host_port_override(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict[str, object] = {}

    def fake_build_server(settings: Settings, **_kw: object) -> _FakeMCP:
        captured["host"] = settings.mcp_host
        captured["port"] = settings.mcp_port
        return _FakeMCP({})

    monkeypatch.setattr(server, "build_server", fake_build_server)
    server.main(["--host", "0.0.0.0", "--port", "9999"])
    assert captured["host"] == "0.0.0.0"
    assert captured["port"] == 9999
