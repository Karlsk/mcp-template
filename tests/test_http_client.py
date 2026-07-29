"""Tests for the generic HTTP client (retry, auth, transport injection)."""

from __future__ import annotations

import logging
from typing import Any

import httpx
import pytest

from app.common.http import (
    AuthConfig,
    HttpClient,
    HttpClientConfig,
    RetryConfig,
)


def _counting_handler(responses: list[httpx.Response | Exception]):
    """Build a MockTransport handler that returns canned responses/errors in order."""
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        index = min(calls, len(responses) - 1)
        item = responses[index]
        calls += 1
        if isinstance(item, Exception):
            raise item
        item.request = request
        return item

    return handler, lambda: calls


async def test_get_success_returns_response() -> None:
    handler, _ = _counting_handler([httpx.Response(200, json={"ok": True})])
    client = HttpClient(
        HttpClientConfig(base_url="https://sdn.example", transport=httpx.MockTransport(handler))
    )
    try:
        resp = await client.get("/health")
    finally:
        await client.close()
    assert resp.status_code == 200
    assert resp.json() == {"ok": True}


async def test_retry_on_5xx_then_success() -> None:
    handler, get_calls = _counting_handler(
        [httpx.Response(500), httpx.Response(200, json={"ok": True})]
    )
    client = HttpClient(
        HttpClientConfig(
            base_url="https://sdn.example",
            transport=httpx.MockTransport(handler),
            retry=RetryConfig(max_retries=3, base_delay=0.0, max_delay=0.0),
        )
    )
    try:
        resp = await client.get("/health")
    finally:
        await client.close()
    assert resp.status_code == 200
    assert get_calls() == 2


async def test_retry_on_429_then_success() -> None:
    handler, get_calls = _counting_handler(
        [httpx.Response(429), httpx.Response(200)]
    )
    client = HttpClient(
        HttpClientConfig(
            base_url="https://sdn.example",
            transport=httpx.MockTransport(handler),
            retry=RetryConfig(max_retries=2, base_delay=0.0, max_delay=0.0),
        )
    )
    try:
        resp = await client.get("/health")
    finally:
        await client.close()
    assert resp.status_code == 200
    assert get_calls() == 2


async def test_no_retry_on_4xx_raises_immediately() -> None:
    """Client errors (401/403/404) must NOT be retried — they will never succeed."""
    handler, get_calls = _counting_handler([httpx.Response(401)])
    client = HttpClient(
        HttpClientConfig(
            base_url="https://sdn.example",
            transport=httpx.MockTransport(handler),
            retry=RetryConfig(max_retries=3, base_delay=0.0, max_delay=0.0),
        )
    )
    try:
        with pytest.raises(httpx.HTTPStatusError):
            await client.get("/health")
    finally:
        await client.close()
    assert get_calls() == 1


async def test_retry_on_transport_error_then_success() -> None:
    handler, get_calls = _counting_handler(
        [httpx.ConnectError("boom"), httpx.Response(200)]
    )
    client = HttpClient(
        HttpClientConfig(
            base_url="https://sdn.example",
            transport=httpx.MockTransport(handler),
            retry=RetryConfig(max_retries=3, base_delay=0.0, max_delay=0.0),
        )
    )
    try:
        resp = await client.get("/health")
    finally:
        await client.close()
    assert resp.status_code == 200
    assert get_calls() == 2


async def test_exhausted_retries_raises_last_error() -> None:
    handler, get_calls = _counting_handler([httpx.Response(500)])
    client = HttpClient(
        HttpClientConfig(
            base_url="https://sdn.example",
            transport=httpx.MockTransport(handler),
            retry=RetryConfig(max_retries=2, base_delay=0.0, max_delay=0.0),
        )
    )
    try:
        with pytest.raises(httpx.HTTPStatusError):
            await client.get("/health")
    finally:
        await client.close()
    # initial attempt + 2 retries = 3 total
    assert get_calls() == 3


async def test_transport_is_injectable() -> None:
    """The client must accept an injectable transport so unit tests need no real server."""
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(str(request.url))
        return httpx.Response(200)

    client = HttpClient(
        HttpClientConfig(base_url="https://sdn.example", transport=httpx.MockTransport(handler))
    )
    try:
        await client.get("/x")
    finally:
        await client.close()
    assert seen == ["https://sdn.example/x"]


async def test_bearer_auth_adds_authorization_header() -> None:
    captured: dict[str, str] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured.update(request.headers)
        return httpx.Response(200)

    client = HttpClient(
        HttpClientConfig(
            base_url="https://sdn.example",
            transport=httpx.MockTransport(handler),
            auth=AuthConfig(auth_type="bearer", bearer_token="secret-token"),
        )
    )
    try:
        await client.get("/x")
    finally:
        await client.close()
    assert captured["authorization"] == "Bearer secret-token"


async def test_basic_auth_uses_basic_auth() -> None:
    captured: dict[str, str] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured.update(request.headers)
        return httpx.Response(200)

    client = HttpClient(
        HttpClientConfig(
            base_url="https://sdn.example",
            transport=httpx.MockTransport(handler),
            auth=AuthConfig(auth_type="basic", username="u", password="p"),
        )
    )
    try:
        await client.get("/x")
    finally:
        await client.close()
    assert captured["authorization"].startswith("Basic ")


async def test_raise_for_status_false_returns_response() -> None:
    handler, _ = _counting_handler([httpx.Response(404)])
    client = HttpClient(
        HttpClientConfig(base_url="https://sdn.example", transport=httpx.MockTransport(handler))
    )
    try:
        resp = await client.get("/x", raise_for_status=False)
    finally:
        await client.close()
    assert resp.status_code == 404


async def test_close_is_idempotent() -> None:
    client = HttpClient(HttpClientConfig())
    await client.close()
    await client.close()  # no error


async def test_async_context_manager_closes_client() -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200)

    async with HttpClient(
        HttpClientConfig(base_url="https://sdn.example", transport=httpx.MockTransport(handler))
    ) as client:
        resp = await client.get("/x")
        assert resp.status_code == 200
    # Exiting the block must have closed the underlying client.
    assert client._client is None or client._client.is_closed


@pytest.mark.parametrize(
    ("method", "call"),
    [
        ("get", lambda c: c.get("/x")),
        ("post", lambda c: c.post("/x", json={"a": 1})),
        ("put", lambda c: c.put("/x", json={"a": 1})),
        ("patch", lambda c: c.patch("/x", json={"a": 1})),
        ("delete", lambda c: c.delete("/x")),
        ("head", lambda c: c.head("/x")),
    ],
)
async def test_method_wrappers_send_correct_verb(method: str, call) -> None:
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.method)
        return httpx.Response(200)

    client = HttpClient(
        HttpClientConfig(base_url="https://sdn.example", transport=httpx.MockTransport(handler))
    )
    try:
        await call(client)
    finally:
        await client.close()
    assert seen == [method.upper()]


async def test_bearer_token_property_returns_configured_token() -> None:
    """bearer_token exposes the configured token without sending a request."""
    client = HttpClient(
        HttpClientConfig(
            base_url="https://sdn.example",
            transport=httpx.MockTransport(lambda _r: httpx.Response(200)),
            auth=AuthConfig(auth_type="bearer", bearer_token="cfg-token"),
        )
    )
    try:
        assert client.bearer_token == "cfg-token"
    finally:
        await client.close()


async def test_set_bearer_token_rotates_header_on_live_client() -> None:
    captured: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(request.headers.get("authorization", ""))
        return httpx.Response(200)

    client = HttpClient(
        HttpClientConfig(
            base_url="https://sdn.example",
            transport=httpx.MockTransport(handler),
            auth=AuthConfig(auth_type="bearer", bearer_token="old"),
        )
    )
    try:
        await client.set_bearer_token("new")
        assert client.bearer_token == "new"
        await client.get("/x")
    finally:
        await client.close()
    assert captured == ["Bearer new"]


async def test_set_bearer_token_empty_removes_header() -> None:
    sent_auth: list[bool] = []

    def handler(request: httpx.Request) -> httpx.Response:
        sent_auth.append("authorization" in request.headers)
        return httpx.Response(200)

    client = HttpClient(
        HttpClientConfig(
            base_url="https://sdn.example",
            transport=httpx.MockTransport(handler),
            auth=AuthConfig(auth_type="bearer", bearer_token="old"),
        )
    )
    try:
        await client.set_bearer_token("")
        await client.get("/x")
    finally:
        await client.close()
    assert sent_auth == [False]


def test_is_retriable_returns_false_for_unrelated_exception() -> None:
    """A non-transport, non-HTTP-status exception is never retried."""
    assert HttpClient._is_retriable(ValueError("not an httpx error")) is False


def _http_records(caplog: pytest.LogCaptureFixture) -> list[Any]:
    return [r for r in caplog.records if r.name == "app.common.http"]


async def test_logs_request_response_metadata_without_body(
    caplog: pytest.LogCaptureFixture,
) -> None:
    handler, _ = _counting_handler([httpx.Response(200, json={"ok": True})])
    client = HttpClient(
        HttpClientConfig(base_url="https://sdn.example", transport=httpx.MockTransport(handler))
    )
    with caplog.at_level(logging.DEBUG, logger="app.common.http"):
        try:
            await client.get("/health")
        finally:
            await client.close()
    records = _http_records(caplog)
    messages = [r.message for r in records]
    assert "http_request" in messages
    assert "http_response" in messages
    resp = next(r for r in records if r.message == "http_response")
    assert resp.status == 200
    assert resp.method == "GET"
    assert resp.url == "/health"
    assert resp.elapsed_ms >= 0.0
    req = next(r for r in records if r.message == "http_request")
    # Bodies are off by default.
    assert getattr(req, "request_body", "missing") is None
    assert getattr(resp, "response_body", "missing") is None


async def test_logs_redacted_bodies_when_enabled(caplog: pytest.LogCaptureFixture) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"data": "ok", "access_token": "resptoken"})

    client = HttpClient(
        HttpClientConfig(
            base_url="https://sdn.example",
            transport=httpx.MockTransport(handler),
            log_bodies=True,
        )
    )
    body = {"username": "u", "password": "supersecret", "access_token": "tok123", "safe": "keep"}
    with caplog.at_level(logging.DEBUG, logger="app.common.http"):
        try:
            await client.post("/login", json=body)
        finally:
            await client.close()
    records = _http_records(caplog)
    req = next(r for r in records if r.message == "http_request")
    resp = next(r for r in records if r.message == "http_response")
    req_json = getattr(req, "request_body", {})["json"]
    resp_body = getattr(resp, "response_body", "")
    assert "supersecret" not in req_json
    assert "tok123" not in req_json
    assert "***" in req_json
    assert "keep" in req_json
    assert "resptoken" not in resp_body
    assert "***" in resp_body


async def test_logs_redacted_error_body_on_http_error(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Error-path body is logged (redacted) at WARNING even with log_bodies off."""
    handler, _ = _counting_handler(
        [httpx.Response(500, json={"error": "db down", "password": "leak"})]
    )
    client = HttpClient(
        HttpClientConfig(
            base_url="https://sdn.example",
            transport=httpx.MockTransport(handler),
            retry=RetryConfig(max_retries=0, base_delay=0.0, max_delay=0.0),
        )
    )
    with caplog.at_level(logging.DEBUG, logger="app.common.http"):
        try:
            with pytest.raises(httpx.HTTPStatusError):
                await client.get("/health")
        finally:
            await client.close()
    errors = [r for r in _http_records(caplog) if r.message == "http_error"]
    assert len(errors) == 1
    err = errors[0]
    assert err.levelno == logging.WARNING
    assert err.status == 500
    body = getattr(err, "response_body", "")
    assert "db down" in body
    assert "leak" not in body
    assert "***" in body


async def test_logs_transport_error(caplog: pytest.LogCaptureFixture) -> None:
    handler, _ = _counting_handler([httpx.ConnectError("boom")])
    client = HttpClient(
        HttpClientConfig(
            base_url="https://sdn.example",
            transport=httpx.MockTransport(handler),
            retry=RetryConfig(max_retries=0, base_delay=0.0, max_delay=0.0),
        )
    )
    with caplog.at_level(logging.DEBUG, logger="app.common.http"):
        try:
            with pytest.raises(httpx.ConnectError):
                await client.get("/health")
        finally:
            await client.close()
    errors = [r for r in _http_records(caplog) if r.message == "http_error"]
    assert len(errors) == 1
    err = errors[0]
    assert err.error == "ConnectError"
    assert getattr(err, "status", "missing") is None
    assert getattr(err, "response_body", "missing") is None


async def test_logs_nonretriable_error_once(caplog: pytest.LogCaptureFixture) -> None:
    handler, get_calls = _counting_handler([httpx.Response(401, json={"msg": "bad creds"})])
    client = HttpClient(
        HttpClientConfig(
            base_url="https://sdn.example",
            transport=httpx.MockTransport(handler),
            retry=RetryConfig(max_retries=3, base_delay=0.0, max_delay=0.0),
        )
    )
    with caplog.at_level(logging.DEBUG, logger="app.common.http"):
        try:
            with pytest.raises(httpx.HTTPStatusError):
                await client.get("/health")
        finally:
            await client.close()
    assert get_calls() == 1
    errors = [r for r in _http_records(caplog) if r.message == "http_error"]
    assert len(errors) == 1
    assert errors[0].status == 401
    assert getattr(errors[0], "retry_in", "missing") is None
