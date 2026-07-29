"""Tests for the SDN client: health probe and error sanitization."""

from __future__ import annotations

import asyncio
import json
from datetime import datetime, timedelta

import httpx
import pytest
from pydantic import SecretStr

from app.sdn.client import PERF_TIME_FORMAT, SDNClient, _default_time_window
from app.sdn.exceptions import (
    SDNAuthError,
    SDNConfigError,
    SDNConnectionError,
    SDNError,
    SDNHTTPError,
    SDNNotFoundError,
)
from app.sdn.models import (
    CommandResultResponse,
    Device,
    LinkInfo,
    OperationLogsResponse,
    PageResponse,
    PerfHistoryResponse,
    SDNAlertsResponse,
    TopologyResponse,
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


# ---------------------------------------------------------------------------
# v1.5 business methods (appended to SDNClient; query_alerts above unchanged)
# ---------------------------------------------------------------------------


def _capturing_handler(payload: object, seen: dict[str, object]) -> httpx.MockTransport:
    """MockTransport that records method/path/params/body and returns ``payload``."""

    def handler(request: httpx.Request) -> httpx.Response:
        seen["method"] = request.method
        seen["path"] = str(request.url.path)
        seen["params"] = dict(request.url.params)
        try:
            seen["body"] = json.loads(request.content)
        except (json.JSONDecodeError, UnicodeDecodeError):
            seen["body"] = None
        return httpx.Response(200, json=payload)

    return httpx.MockTransport(handler)


def _status_handler_for_body(content: bytes) -> httpx.MockTransport:
    """MockTransport returning a 200 with a raw (non-JSON) body, for malformed tests."""
    return httpx.MockTransport(lambda _r: httpx.Response(200, content=content))


def test_default_time_window_fixed_now() -> None:
    start, end = _default_time_window(now=datetime(2026, 7, 29, 12, 0, 0))
    assert start == "2026-07-29 11:00:00"
    assert end == "2026-07-29 12:00:00"


def test_default_time_window_custom_span() -> None:
    start, end = _default_time_window(timedelta(days=1), now=datetime(2026, 7, 29, 12, 0, 0))
    assert start == "2026-07-28 12:00:00"
    assert end == "2026-07-29 12:00:00"


# --- query_devices (§2.2) --------------------------------------------------


async def test_query_devices_builds_body_and_strips_password() -> None:
    seen: dict[str, object] = {}
    transport = _capturing_handler(
        {
            "content": [
                {"id": "d1", "name": "n1", "management-ip": "10.0.0.1", "password": "secret"}
            ]
        },
        seen,
    )
    client = SDNClient(make_settings(), transport=transport)
    try:
        result = await client.query_devices(name="X", pe_as="15200", page_size=5)
    finally:
        await client.aclose()

    assert isinstance(result, PageResponse)
    device = result.content[0]
    assert isinstance(device, Device)
    assert device.management_ip == "10.0.0.1"
    dumped = device.model_dump(by_alias=True)
    assert "password" not in dumped
    # wire contract: POST, controller keys, peAs camelCase, None filters omitted
    assert seen["method"] == "POST"
    assert seen["path"] == "/api/no/config/terra-pe:peInfos/page"
    assert seen["params"] == {"pageNumber": "1", "pageSize": "5"}
    assert seen["body"] == {"name": "X", "peAs": "15200"}


async def test_query_devices_uses_configured_endpoint() -> None:
    sdn = SdnSettings(
        base_url=BASE_URL,
        auth_type="bearer",
        timeout=1.0,
        retry=RetrySettings(max_retries=0, base_delay=0.0, max_delay=0.0),
        endpoints={"health": "/", "devices_page": "/custom/devices"},
    )
    settings = Settings(sdn=sdn, sdn_controller_token=SecretStr("tok"))
    seen: dict[str, object] = {}
    client = SDNClient(settings, transport=_capturing_handler({"content": []}, seen))
    try:
        await client.query_devices()
    finally:
        await client.aclose()
    assert seen["path"] == "/custom/devices"


async def test_query_devices_malformed_raises_sdn_error() -> None:
    client = SDNClient(make_settings(), transport=_status_handler_for_body(b"not-json"))
    try:
        with pytest.raises(SDNError):
            await client.query_devices()
    finally:
        await client.aclose()


async def test_query_devices_server_error_raises_http_error() -> None:
    client = SDNClient(make_settings(), transport=_status_handler(500))
    try:
        with pytest.raises(SDNHTTPError):
            await client.query_devices()
    finally:
        await client.aclose()


# --- query_links (§2.16) ---------------------------------------------------


async def test_query_links_converts_page_num_and_omits_none() -> None:
    seen: dict[str, object] = {}
    transport = _capturing_handler(
        {"content": [{"link-id": "L1", "link-status": "UP"}], "total_elements": 1, "number": 1},
        seen,
    )
    client = SDNClient(make_settings(), transport=transport)
    try:
        result = await client.query_links(page_num=2, link_id="L1")
    finally:
        await client.aclose()

    link = result.content[0]
    assert isinstance(link, LinkInfo)
    assert link.link_id == "L1"
    assert seen["method"] == "GET"
    assert seen["path"] == (
        "/api/sr/config/network-topology:network-topology/topology/linksInfo/page"
    )
    params = seen["params"]
    assert params["page"] == "1"  # 1-based page_num=2 -> 0-based page=1
    assert params["size"] == "10"
    assert params["linkId"] == "L1"
    assert "linkErr" not in params  # None filtered out (httpx would else emit "")


async def test_query_links_serializes_bool_filter() -> None:
    seen: dict[str, object] = {}
    client = SDNClient(make_settings(), transport=_capturing_handler({"content": []}, seen))
    try:
        await client.query_links(link_err=True)
    finally:
        await client.aclose()
    assert seen["params"]["linkErr"] == "true"


async def test_query_links_malformed_raises_sdn_error() -> None:
    client = SDNClient(make_settings(), transport=_status_handler_for_body(b"not-json"))
    try:
        with pytest.raises(SDNError):
            await client.query_links()
    finally:
        await client.aclose()


# --- performance history (§3.2 / §3.3 / §3.4) ------------------------------


def _perf_payload() -> dict[str, object]:
    return {"code": 0, "message": "ok", "data": [{"time": "t", "in_traffic": 1.0}]}


async def test_query_switch_history_port_dimensions() -> None:
    seen: dict[str, object] = {}
    client = SDNClient(make_settings(), transport=_capturing_handler(_perf_payload(), seen))
    try:
        result = await client.query_switch_history(
            "port",
            metric_names=["in_traffic", "out_traffic"],
            device_name="SW-1",
            port_name="GE1",
            start_time="2026-07-29 10:00:00",
            end_time="2026-07-29 11:00:00",
        )
    finally:
        await client.aclose()

    assert isinstance(result, PerfHistoryResponse)
    assert result.data[0].time == "t"
    params = seen["params"]
    assert seen["path"] == "/monitor/switch/history"
    assert params["namespace"] == "port"
    assert params["metricNames"] == "in_traffic,out_traffic"
    assert params["dimensions.0.name"] == "switch"
    assert params["dimensions.0.value"] == "SW-1"
    assert params["dimensions.1.name"] == "port"
    assert params["dimensions.1.value"] == "GE1"
    assert params["startTime"] == "2026-07-29 10:00:00"
    assert params["endTime"] == "2026-07-29 11:00:00"


async def test_query_switch_history_link_dimensions() -> None:
    seen: dict[str, object] = {}
    client = SDNClient(make_settings(), transport=_capturing_handler(_perf_payload(), seen))
    try:
        await client.query_switch_history(
            "link", metric_names=["jitter"], link_id="L1",
            start_time="s", end_time="e",
        )
    finally:
        await client.aclose()
    params = seen["params"]
    assert params["namespace"] == "link"
    assert params["dimensions.0.name"] == "linkId"
    assert params["dimensions.0.value"] == "L1"


async def test_query_switch_history_port_requires_device_and_port() -> None:
    client = SDNClient(make_settings(), transport=_capturing_handler(_perf_payload(), {}))
    try:
        with pytest.raises(SDNError):
            await client.query_switch_history(
                "port", metric_names=["in_traffic"], device_name="SW-1",
                start_time="s", end_time="e",
            )
    finally:
        await client.aclose()


async def test_query_switch_history_default_window_is_last_hour() -> None:
    seen: dict[str, object] = {}
    client = SDNClient(make_settings(), transport=_capturing_handler(_perf_payload(), seen))
    try:
        await client.query_switch_history(
            "port", metric_names=["in_traffic"], device_name="SW-1", port_name="GE1",
        )
    finally:
        await client.aclose()
    start = datetime.strptime(seen["params"]["startTime"], PERF_TIME_FORMAT)
    end = datetime.strptime(seen["params"]["endTime"], PERF_TIME_FORMAT)
    assert end - start == timedelta(hours=1)


async def test_query_vpn_history_dimensions() -> None:
    seen: dict[str, object] = {}
    client = SDNClient(make_settings(), transport=_capturing_handler(_perf_payload(), seen))
    try:
        await client.query_vpn_history(
            "l2_3510", metric_names=["in_traffic", "out_traffic"], start_time="s", end_time="e",
        )
    finally:
        await client.aclose()
    assert seen["path"] == "/monitor/vpn/history"
    params = seen["params"]
    assert params["namespace"] == "traffic"
    assert params["dimensions.0.name"] == "vpnId"
    assert params["dimensions.0.value"] == "l2_3510"


async def test_query_te_history_dimensions() -> None:
    seen: dict[str, object] = {}
    client = SDNClient(make_settings(), transport=_capturing_handler(_perf_payload(), seen))
    try:
        await client.query_te_history(
            "PE-1", "Tunnel5043", metric_names=["in_traffic"], start_time="s", end_time="e",
        )
    finally:
        await client.aclose()
    assert seen["path"] == "/monitor/te/history"
    params = seen["params"]
    assert params["namespace"] == "traffic"
    assert params["dimensions.0.name"] == "deviceName"
    assert params["dimensions.0.value"] == "PE-1"
    assert params["dimensions.1.name"] == "tunnelName"
    assert params["dimensions.1.value"] == "Tunnel5043"


# --- query_alert_page (§3.5, NEW; reuses SDNAlertsResponse) ----------------


async def test_query_alert_page_default_body_has_window_and_no_optionals() -> None:
    seen: dict[str, object] = {}
    client = SDNClient(make_settings(), transport=_capturing_handler(_alerts_body(), seen))
    try:
        result = await client.query_alert_page()
    finally:
        await client.aclose()

    assert isinstance(result, SDNAlertsResponse)
    assert seen["path"] == "/monitor/v2/alert/page"
    body = seen["body"]
    assert set(body) == {"startTime", "endTime", "pageNum", "pageSize"}
    assert body["pageNum"] == 1
    assert body["pageSize"] == 10


async def test_query_alert_page_includes_optionals_when_set() -> None:
    seen: dict[str, object] = {}
    client = SDNClient(make_settings(), transport=_capturing_handler(_alerts_body(), seen))
    try:
        await client.query_alert_page(
            namespace="device",
            category="ISIS邻居Down",
            source_list=["NJ-SCT-R03"],
            auto_recovery=1,
            msg="PolicyPath",
            level="CRITICAL",
        )
    finally:
        await client.aclose()
    body = seen["body"]
    assert body["namespace"] == "device"
    assert body["category"] == "ISIS邻居Down"
    assert body["sourceList"] == ["NJ-SCT-R03"]
    assert body["autoRecovery"] == 1
    assert body["msg"] == "PolicyPath"
    assert body["level"] == "CRITICAL"


async def test_query_alert_page_malformed_raises_sdn_error() -> None:
    client = SDNClient(make_settings(), transport=_status_handler_for_body(b"not-json"))
    try:
        with pytest.raises(SDNError):
            await client.query_alert_page()
    finally:
        await client.aclose()


# --- get_topology (§3.7) ---------------------------------------------------


async def test_get_topology_parses_response() -> None:
    seen: dict[str, object] = {}
    payload = {"topology": [{"topology-id": "t1", "node": [], "link": []}]}
    client = SDNClient(make_settings(), transport=_capturing_handler(payload, seen))
    try:
        result = await client.get_topology()
    finally:
        await client.aclose()
    assert isinstance(result, TopologyResponse)
    assert result.topology[0].topology_id == "t1"
    assert seen["method"] == "GET"
    assert seen["path"] == "/api/sr/config/network-topology:network-topology"


async def test_get_topology_malformed_raises_sdn_error() -> None:
    client = SDNClient(make_settings(), transport=_status_handler_for_body(b"not-json"))
    try:
        with pytest.raises(SDNError):
            await client.get_topology()
    finally:
        await client.aclose()


# --- query_operation_logs (§3.12) ------------------------------------------


async def test_query_operation_logs_builds_params_and_parses() -> None:
    seen: dict[str, object] = {}
    payload = {
        "code": 0,
        "message": "ok",
        "data": [{"id": "l1", "time": "t", "caller": "c"}],
    }
    client = SDNClient(make_settings(), transport=_capturing_handler(payload, seen))
    try:
        result = await client.query_operation_logs(
            start_time="2026-04-21 15:53:46",
            end_time="2026-04-21 16:53:46",
            page_num=2,
            page_size=5,
        )
    finally:
        await client.aclose()

    assert isinstance(result, OperationLogsResponse)
    assert result.data[0].caller == "c"
    assert seen["method"] == "GET"
    assert seen["path"] == "/monitor/logs"
    assert seen["params"] == {
        "startTime": "2026-04-21 15:53:46",
        "endTime": "2026-04-21 16:53:46",
        "pageNum": "2",
        "pageSize": "5",
    }


async def test_query_operation_logs_default_window() -> None:
    seen: dict[str, object] = {}
    client = SDNClient(make_settings(), transport=_capturing_handler({"data": []}, seen))
    try:
        await client.query_operation_logs()
    finally:
        await client.aclose()
    start = datetime.strptime(seen["params"]["startTime"], PERF_TIME_FORMAT)
    end = datetime.strptime(seen["params"]["endTime"], PERF_TIME_FORMAT)
    assert end - start == timedelta(hours=1)


async def test_query_operation_logs_malformed_raises_sdn_error() -> None:
    client = SDNClient(make_settings(), transport=_status_handler_for_body(b"not-json"))
    try:
        with pytest.raises(SDNError):
            await client.query_operation_logs()
    finally:
        await client.aclose()


async def test_business_request_sends_accept_json_header() -> None:
    """Every business request carries Accept: application/json.

    The controller's /api/... REST endpoints return HTTP 415 (Unsupported Media
    Type) without it, so the header is injected once in the request() primitive.
    """
    seen: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["accept"] = request.headers.get("accept")
        return httpx.Response(200, json={"topology": []})

    client = SDNClient(make_settings(), transport=httpx.MockTransport(handler))
    try:
        await client.get_topology()
    finally:
        await client.aclose()
    assert seen["accept"] == "application/json"


async def test_business_request_sends_content_type_on_bodyless_get() -> None:
    """Bodyless GETs also carry Content-Type: application/json.

    The controller's /api/* config module enforces Content-Type on every
    method (415 Unsupported Media Type without it), even on a GET with no body.
    httpx only auto-adds Content-Type when a body is present, so it is injected
    once in the request() primitive alongside Accept.
    """
    seen: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["content_type"] = request.headers.get("content-type")
        return httpx.Response(200, json={"topology": []})

    client = SDNClient(make_settings(), transport=httpx.MockTransport(handler))
    try:
        await client.get_topology()
    finally:
        await client.aclose()
    assert seen["content_type"] == "application/json"


# --- run_command (POST /api/no/config/device-conf/command-result) -----------


async def test_run_command_posts_body_and_parses_result() -> None:
    seen: dict[str, object] = {}
    transport = _capturing_handler({"result": "dis ip in br\r\r\nGE4/1/1 up"}, seen)
    client = SDNClient(make_settings(), transport=transport)
    try:
        result = await client.run_command("NJ-SCT-R01", "dis ip in br")
    finally:
        await client.aclose()

    assert isinstance(result, CommandResultResponse)
    assert result.result.startswith("dis ip in br")
    # wire contract: POST, the documented endpoint, snake_case body keys
    assert seen["method"] == "POST"
    assert seen["path"] == "/api/no/config/device-conf/command-result"
    assert seen["body"] == {"device_name": "NJ-SCT-R01", "command": "dis ip in br"}


async def test_run_command_malformed_raises_sdn_error() -> None:
    client = SDNClient(make_settings(), transport=_status_handler_for_body(b"not-json"))
    try:
        with pytest.raises(SDNError):
            await client.run_command("NJ-SCT-R01", "display version")
    finally:
        await client.aclose()
