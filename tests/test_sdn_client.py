"""Tests for the SDN client: health probe and error sanitization."""

from __future__ import annotations

import asyncio
import json

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
from app.sdn.models import SDNAlertsResponse
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
    return Settings(sdn=sdn, sdn_controller_token=SecretStr("tok"))


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


# ---------------------------------------------------------------------------
# Token refresh (auth_type=basic only)
#
# basic mode = obtain a bearer token via a Basic-Auth login, use it for data
# requests, and refresh it on HTTP 401. Business code is unaware of tokens:
# every method goes through SDNClient._send, which handles all three
# auth_types (no-auth / bearer / basic) transparently.
# ---------------------------------------------------------------------------


def make_basic_settings(
    *, base_url: str = BASE_URL, token_field: str = "access_token"
) -> Settings:
    """Settings wired for auth_type=basic (login endpoint + token refresh)."""
    sdn = SdnSettings(
        base_url=base_url,
        auth_type="basic",
        timeout=1.0,
        retry=RetrySettings(max_retries=0, base_delay=0.0, max_delay=0.0),
        endpoints={"health": "/", "login": "/login"},
        token_field=token_field,
    )
    return Settings(sdn=sdn, sdn_controller_username="u", sdn_controller_password=SecretStr("p"))


def _sequence_transport(responses: list[httpx.Response | Exception]):
    """MockTransport returning canned responses/errors in order; reports call count."""
    counter = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        idx = min(counter["n"], len(responses) - 1)
        item = responses[idx]
        counter["n"] += 1
        if isinstance(item, Exception):
            raise item
        item.request = request
        return item

    return httpx.MockTransport(handler), lambda: counter["n"]


def _login_transport(tokens: list[str] | None = None, *, fail: bool = False):
    """MockTransport emulating the login endpoint (no-auth, body creds, JSON token)."""
    calls = {"n": 0}
    token_iter = iter(tokens) if tokens else None

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        # login is no-auth: credentials travel in the JSON body, never a header
        assert "authorization" not in request.headers, "login must not send an auth header"
        body = json.loads(request.content)
        assert {"username", "password", "device_id"} <= set(body), (
            "login body must include username, password, device_id"
        )
        if fail:
            raise httpx.ConnectError("login endpoint down")
        return httpx.Response(200, json={"access_token": next(token_iter) if token_iter else "tok"})

    return httpx.MockTransport(handler), lambda: calls["n"]


async def test_basic_initialize_fetches_first_token() -> None:
    """initialize() logs in once via Basic Auth and seeds the data client's token."""
    main_seen: list[str] = []

    def main_handler(request: httpx.Request) -> httpx.Response:
        main_seen.append(request.headers.get("authorization", ""))
        return httpx.Response(200)

    login, login_calls = _login_transport(tokens=["tok-1"])
    client = SDNClient(
        make_basic_settings(),
        transport=httpx.MockTransport(main_handler),
        login_transport=login,
    )
    try:
        await client.initialize()
        assert client._http.bearer_token == "tok-1"
        assert login_calls() == 1
        await client.health()
    finally:
        await client.aclose()
    # The data request reused the init token; no extra login happened.
    assert main_seen == ["Bearer tok-1"]
    assert login_calls() == 1


async def test_basic_refresh_on_401_then_success() -> None:
    main, main_calls = _sequence_transport([httpx.Response(401), httpx.Response(200)])
    login, login_calls = _login_transport(tokens=["tok-1", "tok-2"])
    client = SDNClient(make_basic_settings(), transport=main, login_transport=login)
    try:
        await client.initialize()
        result = await client.health()
    finally:
        await client.aclose()
    assert result.status_code == 200
    assert client._http.bearer_token == "tok-2"
    assert main_calls() == 2  # initial 401 + post-refresh 200
    assert login_calls() == 2  # init + one refresh


async def test_basic_refresh_only_once_then_raises_auth_error() -> None:
    """A second 401 after refresh must NOT refresh again — raise SDNAuthError."""
    main, main_calls = _sequence_transport([httpx.Response(401), httpx.Response(401)])
    login, login_calls = _login_transport(tokens=["tok-1", "tok-2"])
    client = SDNClient(make_basic_settings(), transport=main, login_transport=login)
    try:
        await client.initialize()
        with pytest.raises(SDNAuthError):
            await client.health()
    finally:
        await client.aclose()
    assert main_calls() == 2  # initial 401 + post-refresh 401
    assert login_calls() == 2  # init + exactly one refresh (no second refresh)


async def test_basic_concurrent_401_collapse_to_single_refresh() -> None:
    """N concurrent 401s must trigger exactly one refresh login (no stampede)."""
    main_calls = {"n": 0}

    def main_handler(request: httpx.Request) -> httpx.Response:
        main_calls["n"] += 1
        # expired token -> 401; refreshed token -> 200
        if request.headers.get("authorization") == "Bearer tok-1":
            return httpx.Response(401)
        return httpx.Response(200)

    login, login_calls = _login_transport(tokens=["tok-1", "tok-2"])
    client = SDNClient(
        make_basic_settings(),
        transport=httpx.MockTransport(main_handler),
        login_transport=login,
    )
    try:
        await client.initialize()
        results = await asyncio.gather(*(client.health() for _ in range(3)))
    finally:
        await client.aclose()
    assert all(r.status_code == 200 for r in results)
    assert login_calls() == 2  # init + one refresh despite 3 concurrent 401s


async def test_basic_login_failure_propagates_without_recursion() -> None:
    """Login endpoint failure -> SDNError; login hit exactly once (no recursion)."""
    main_calls = {"n": 0}

    def main_handler(request: httpx.Request) -> httpx.Response:
        main_calls["n"] += 1
        return httpx.Response(401)

    login, login_calls = _login_transport(fail=True)
    client = SDNClient(
        make_basic_settings(),
        transport=httpx.MockTransport(main_handler),
        login_transport=login,
    )
    try:
        with pytest.raises(SDNError):
            await client.initialize()
    finally:
        await client.aclose()
    assert login_calls() == 1  # failed login, no recursive retry
    assert main_calls["n"] == 0  # no data request was made during a failed init


async def test_basic_login_failure_negative_cache() -> None:
    """Within the cooldown, a second refresh must NOT re-hit the login endpoint."""
    main, _main_calls = _sequence_transport(
        [httpx.Response(401), httpx.Response(401), httpx.Response(401)]
    )
    login_calls = {"n": 0}
    fail = {"on": False}

    def login_handler(request: httpx.Request) -> httpx.Response:
        login_calls["n"] += 1
        assert "authorization" not in request.headers
        assert {"username", "password", "device_id"} <= set(json.loads(request.content))
        if fail["on"]:
            raise httpx.ConnectError("login endpoint down")
        return httpx.Response(200, json={"access_token": "tok-1"})

    client = SDNClient(
        make_basic_settings(), transport=main, login_transport=httpx.MockTransport(login_handler)
    )
    try:
        await client.initialize()  # login ok -> tok-1 (login_calls=1)
        fail["on"] = True
        with pytest.raises(SDNError):  # 401 -> refresh -> login fails (login_calls=2)
            await client.health()
        with pytest.raises(SDNError):  # 401 -> refresh -> negative-cache hit
            await client.health()
    finally:
        await client.aclose()
    assert login_calls["n"] == 2  # init + one failed refresh; second refresh served from cache


@pytest.mark.parametrize("login_body", [{"unexpected": "x"}, b"not-json", [1, 2, 3], "astring", 42])
async def test_basic_malformed_login_response_raises_auth_error(login_body: object) -> None:
    """Missing token field, non-JSON, or non-dict JSON all surface as SDNAuthError."""
    main = httpx.MockTransport(lambda _r: httpx.Response(200))

    def login_handler(request: httpx.Request) -> httpx.Response:
        if isinstance(login_body, bytes):
            return httpx.Response(200, content=login_body)
        return httpx.Response(200, json=login_body)

    client = SDNClient(
        make_basic_settings(), transport=main, login_transport=httpx.MockTransport(login_handler)
    )
    try:
        with pytest.raises(SDNAuthError) as exc_info:
            await client.initialize()
    finally:
        await client.aclose()
    assert BASE_URL not in str(exc_info.value)  # sanitized public message


async def test_basic_missing_credentials_raises_config_error() -> None:
    """basic mode without username/password/login endpoint fails fast at construction."""
    sdn = SdnSettings(
        base_url=BASE_URL,
        auth_type="basic",
        timeout=1.0,
        retry=RetrySettings(max_retries=0, base_delay=0.0, max_delay=0.0),
        endpoints={"health": "/"},  # no login endpoint
    )
    settings = Settings(sdn=sdn)  # no username/password
    with pytest.raises(SDNConfigError):
        SDNClient(settings)


async def test_basic_mode_unconfigured_boots_without_credentials() -> None:
    """basic mode with no base_url (skeleton) still constructs and reports unconfigured."""
    sdn = SdnSettings(
        base_url="",
        auth_type="basic",
        timeout=1.0,
        retry=RetrySettings(max_retries=0, base_delay=0.0, max_delay=0.0),
        endpoints={"health": "/", "login": "/login"},
    )
    settings = Settings(sdn=sdn)  # no base_url, no credentials
    client = SDNClient(settings)
    try:
        assert client.configured is False
        assert client._login_http is None  # no login client wired (unconfigured)
        await client.initialize()  # no-op when unconfigured
        with pytest.raises(SDNConfigError):
            await client.health()
    finally:
        await client.aclose()


async def test_basic_aclose_closes_both_clients() -> None:
    main = httpx.MockTransport(lambda _r: httpx.Response(200))
    login = httpx.MockTransport(lambda _r: httpx.Response(200, json={"access_token": "t"}))
    client = SDNClient(make_basic_settings(), transport=main, login_transport=login)
    await client.initialize()
    await client.aclose()
    assert client._http._client is None or client._http._client.is_closed
    assert client._login_http is not None
    assert client._login_http._client is None or client._login_http._client.is_closed


async def test_bearer_mode_does_not_refresh_on_401() -> None:
    """bearer (fixed api-key) must never log in or refresh; 401 just raises."""
    main, main_calls = _sequence_transport([httpx.Response(401)])
    client = SDNClient(make_settings(), transport=main)  # bearer, no login client
    try:
        with pytest.raises(SDNAuthError):
            await client.health()
    finally:
        await client.aclose()
    assert main_calls() == 1
    assert client._login_http is None


async def test_refresh_skips_when_generation_advanced() -> None:
    """If another caller already refreshed (generation advanced), _refresh_token is a no-op.

    This is the core of the stampede fix: dedup is generation-based, so it
    collapses concurrent refreshes even when the refreshed token string equals
    the expired one (a value-equality check could not tell them apart). True
    async interleaving isn't reliably exercisable via the synchronous
    MockTransport, so the dedup branch is exercised directly here.
    """
    main = httpx.MockTransport(lambda _r: httpx.Response(200))
    login, login_calls = _login_transport(tokens=["tok-1"])
    client = SDNClient(make_basic_settings(), transport=main, login_transport=login)
    try:
        await client.initialize()  # gen 0 -> 1 (login_calls=1)
        stale_gen = client._refresh_gen
        client._refresh_gen = stale_gen + 1  # simulate a concurrent refresh completing
        await client._refresh_token(stale_gen)  # generation advanced -> skip, no login
    finally:
        await client.aclose()
    assert login_calls() == 1


async def test_basic_non_401_status_error_propagates_without_refresh() -> None:
    """In basic mode a 500 propagates (-> SDNHTTPError) without triggering refresh."""
    main, main_calls = _sequence_transport([httpx.Response(500)])
    login, login_calls = _login_transport(tokens=["tok-1"])
    client = SDNClient(make_basic_settings(), transport=main, login_transport=login)
    try:
        await client.initialize()
        with pytest.raises(SDNHTTPError):
            await client.health()
    finally:
        await client.aclose()
    assert main_calls() == 1
    assert login_calls() == 1  # init only; a 500 does not refresh


async def test_lifespan_closes_clients_when_initialize_fails() -> None:
    """If initialize() raises, the lifespan still closes the constructed clients."""
    from app.server import _make_lifespan

    def login_handler(_request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("login endpoint down")

    made: dict[str, SDNClient] = {}

    def factory() -> SDNClient:
        client = SDNClient(
            make_basic_settings(),
            transport=httpx.MockTransport(lambda _r: httpx.Response(200)),
            login_transport=httpx.MockTransport(login_handler),
        )
        made["client"] = client
        return client

    lifespan = _make_lifespan(factory)
    with pytest.raises(SDNError):
        async with lifespan(None):
            pass  # pragma: no cover  # initialize() raises before yield

    client = made["client"]
    # initialize() raised, but the lifespan's finally still closed both clients.
    assert client._http._client is None or client._http._client.is_closed
    assert client._login_http is not None
    assert client._login_http._client is None or client._login_http._client.is_closed


async def test_request_is_the_business_primitive() -> None:
    """request() routes a business call through _send and returns the response."""
    seen: list[tuple[str, str]] = []

    def main_handler(request: httpx.Request) -> httpx.Response:
        seen.append((request.method, str(request.url.path)))
        return httpx.Response(200, json={"items": [1, 2, 3]})

    login, login_calls = _login_transport(tokens=["tok-1"])
    client = SDNClient(
        make_basic_settings(), transport=httpx.MockTransport(main_handler), login_transport=login
    )
    try:
        await client.initialize()
        resp = await client.request("POST", "/monitor/v2/alert/page", json={"interval": "1h"})
    finally:
        await client.aclose()
    assert resp.status_code == 200
    assert resp.json() == {"items": [1, 2, 3]}
    assert seen == [("POST", "/monitor/v2/alert/page")]
    assert login_calls() == 1  # token still valid, no refresh


async def test_request_maps_status_error_to_sdn_error() -> None:
    """A failing business call surfaces as a sanitized SDNError, never a raw httpx error."""
    main, _main_calls = _sequence_transport([httpx.Response(500)])
    login, _login_calls = _login_transport(tokens=["tok-1"])
    client = SDNClient(make_basic_settings(), transport=main, login_transport=login)
    try:
        await client.initialize()
        with pytest.raises(SDNHTTPError):
            await client.request("GET", "/x")
    finally:
        await client.aclose()


# ---------------------------------------------------------------------------
# query_alerts business method (endpoint + body + boundary validation in client)
# ---------------------------------------------------------------------------


def _alerts_body(total: int = 1) -> dict[str, object]:
    return {
        "success": True,
        "total": total,
        "message": "请求成功",
        "code": 0,
        "data": [
            {
                "id": "a1",
                "category": "PE端口Down",
                "source": "SW-1",
                "component": "Eth1",
                "level": "CRITICAL",
                "msg": "port down",
                "time": "2026-01-01",
                "traceId": "collector/x/1",  # extra field, must be preserved
            }
        ],
    }


async def test_query_alerts_success_validates_and_preserves_extra() -> None:
    """query_alerts parses into a typed model, builds the body, preserves extras."""
    seen: dict[str, object] = {}

    def main_handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url.path)
        seen["body"] = json.loads(request.content)
        return httpx.Response(200, json=_alerts_body(total=1))

    client = SDNClient(make_settings(), transport=httpx.MockTransport(main_handler))
    try:
        result = await client.query_alerts(interval="2h", page_size=5)
    finally:
        await client.aclose()

    assert isinstance(result, SDNAlertsResponse)
    assert result.total == 1
    assert result.success is True
    assert result.code == 0
    assert result.data[0].source == "SW-1"
    # extra (untyped) field survives via extra="allow"
    assert result.data[0].model_dump()["traceId"] == "collector/x/1"
    # the client owns the endpoint + request body (business logic in the client)
    assert seen["url"] == "/monitor/v2/alert/page"
    assert seen["body"] == {
        "interval": "2h",
        "namespace": "device",
        "category": "PE端口Down",
        "pageNum": 1,
        "pageSize": 5,
    }


async def test_query_alerts_uses_configured_endpoint() -> None:
    sdn = SdnSettings(
        base_url=BASE_URL,
        auth_type="bearer",
        timeout=1.0,
        retry=RetrySettings(max_retries=0, base_delay=0.0, max_delay=0.0),
        endpoints={"health": "/", "alerts": "/custom/alerts"},
    )
    settings = Settings(sdn=sdn, sdn_controller_token=SecretStr("tok"))
    seen: list[str] = []

    def main_handler(request: httpx.Request) -> httpx.Response:
        seen.append(str(request.url.path))
        return httpx.Response(200, json=_alerts_body())

    client = SDNClient(settings, transport=httpx.MockTransport(main_handler))
    try:
        await client.query_alerts()
    finally:
        await client.aclose()
    assert seen == ["/custom/alerts"]


async def test_query_alerts_malformed_response_raises_sdn_error() -> None:
    """A non-JSON / non-conforming response surfaces as a sanitized SDNError."""
    main = httpx.MockTransport(lambda _r: httpx.Response(200, content=b"not-json"))
    client = SDNClient(make_settings(), transport=main)
    try:
        with pytest.raises(SDNError):
            await client.query_alerts()
    finally:
        await client.aclose()


async def test_query_alerts_refreshes_token_on_401() -> None:
    """basic mode: a 401 on the alerts call triggers a login + retry (transparent)."""
    main, main_calls = _sequence_transport(
        [httpx.Response(401), httpx.Response(200, json=_alerts_body())]
    )
    login, login_calls = _login_transport(tokens=["tok-1", "tok-2"])
    client = SDNClient(make_basic_settings(), transport=main, login_transport=login)
    try:
        await client.initialize()
        result = await client.query_alerts()
    finally:
        await client.aclose()
    assert result.total == 1
    assert main_calls() == 2  # 401 + retry
    assert login_calls() == 2  # init + one refresh
