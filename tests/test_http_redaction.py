"""Unit tests for the HTTP log redaction/truncation helpers.

These are module-level pure functions (no HttpClient needed), tested directly so
the redaction contract — the security-critical part of body logging — is pinned.
"""

from __future__ import annotations

import httpx

from app.common.http import (
    _redact,
    _redact_request_body,
    _redact_response_body,
    _truncate_text,
)


def test_redact_masks_sensitive_keys_recursively() -> None:
    data = {
        "username": "u",
        "password": "p",
        "access_token": "t",
        "items": [{"api_key": "k", "n": 1}],
    }
    out = _redact(data, ())
    assert out["username"] == "u"
    assert out["password"] == "***"
    assert out["access_token"] == "***"
    assert out["items"][0]["api_key"] == "***"
    assert out["items"][0]["n"] == 1


def test_redact_uses_extra_sensitive_keys() -> None:
    out = _redact({"sessionId": "abc", "keep": "v"}, ("sessionId",))
    assert out["sessionId"] == "***"
    assert out["keep"] == "v"


def test_truncate_text_appends_suffix_over_limit() -> None:
    assert _truncate_text("short", 10) == "short"
    out = _truncate_text("x" * 50, 10)
    assert out.startswith("x" * 10)
    assert "50 bytes" in out


def test_redact_request_body_handles_json_params_and_content() -> None:
    out = _redact_request_body(
        json_body={"password": "p"},
        params={"q": "v", "token": "t"},
        content=b"raw bytes here",
        limit=2048,
        extra_sensitive=(),
    )
    assert out is not None
    assert '"password": "***"' in out["json"]
    assert out["params"] == {"q": "v", "token": "***"}
    assert out["content"] == "raw bytes here"


def test_redact_request_body_returns_none_when_empty() -> None:
    assert _redact_request_body(None, None, None, 2048, ()) is None


def test_redact_response_body_falls_back_to_text_for_non_json() -> None:
    resp = httpx.Response(500, content=b"<html>boom</html>")
    out = _redact_response_body(resp, 2048, ())
    assert out is not None
    assert "boom" in out
