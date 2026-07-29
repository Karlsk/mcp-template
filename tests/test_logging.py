"""Tests for the centralized logging configuration."""

from __future__ import annotations

import json
import logging
import sys

from app.common.logging import (
    APP_LOGGER_NAME,
    JsonFormatter,
    _AppStreamHandler,
    get_logger,
    setup_logging,
)


def test_setup_logging_sets_level() -> None:
    logger = setup_logging("DEBUG")
    assert logger.name == APP_LOGGER_NAME
    assert logger.level == logging.DEBUG


def test_setup_logging_default_level_is_info() -> None:
    logger = setup_logging()
    assert logger.level == logging.INFO


def test_setup_logging_disables_propagation() -> None:
    logger = setup_logging("INFO")
    assert logger.propagate is False


def test_setup_logging_is_idempotent() -> None:
    setup_logging("INFO")
    setup_logging("DEBUG")
    logger = logging.getLogger(APP_LOGGER_NAME)
    handlers = [h for h in logger.handlers if isinstance(h, _AppStreamHandler)]
    assert len(handlers) == 1


def test_json_formatter_emits_json_with_extra_fields() -> None:
    record = logging.LogRecord(
        "app.common.http", logging.DEBUG, __file__, 1, "http_request", (), None
    )
    record.method = "GET"  # type: ignore[attr-defined]
    record.url = "/health"  # type: ignore[attr-defined]
    payload = json.loads(JsonFormatter().format(record))
    assert payload["level"] == "DEBUG"
    assert payload["logger"] == "app.common.http"
    assert payload["message"] == "http_request"
    assert "timestamp" in payload
    assert payload["method"] == "GET"
    assert payload["url"] == "/health"


def test_json_formatter_includes_exc_info() -> None:
    try:
        raise ValueError("boom")
    except ValueError:
        exc_info = sys.exc_info()
    record = logging.LogRecord("app.test", logging.ERROR, __file__, 1, "failed", (), exc_info)
    payload = json.loads(JsonFormatter().format(record))
    assert "ValueError" in payload["exc_info"]


def test_json_formatter_skips_private_extra_keys() -> None:
    record = logging.LogRecord("app.test", logging.INFO, __file__, 1, "hi", (), None)
    record._internal = "hidden"  # type: ignore[attr-defined]
    payload = json.loads(JsonFormatter().format(record))
    assert "_internal" not in payload


def test_get_logger_defaults_to_app_namespace() -> None:
    assert get_logger().name == "app"
    assert get_logger("app.sdn.client").name == "app.sdn.client"
