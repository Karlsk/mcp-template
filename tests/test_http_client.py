"""Tests for the generic HTTP client (retry, auth, transport injection)."""

from __future__ import annotations

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
