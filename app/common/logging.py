"""Central logging configuration for the sdn-mcp server.

All application loggers live under the ``app`` namespace and emit one JSON object
per line to stderr. :func:`setup_logging` installs a single handler on the
``app`` logger and applies the configured level; it is idempotent and called once
at process start (see :func:`app.server.main`). Existing modules already use
``logging.getLogger(__name__)``, so every ``app.*`` logger inherits this handler
and level automatically — no per-module wiring is required.

Server-side only. These logs may contain URLs and, when explicitly enabled,
redacted request/response bodies. Never forward them to a channel the model can
read — the SDN error layer sanitizes what reaches the agent.
"""

from __future__ import annotations

import json
import logging
import sys
from datetime import UTC, datetime
from typing import Any, Final, TextIO

APP_LOGGER_NAME: Final[str] = "app"
DEFAULT_LEVEL: Final[str] = "INFO"

_LEVELS: Final[dict[str, int]] = {
    "DEBUG": logging.DEBUG,
    "INFO": logging.INFO,
    "WARNING": logging.WARNING,
    "ERROR": logging.ERROR,
    "CRITICAL": logging.CRITICAL,
}

# Standard :class:`logging.LogRecord` attributes. Everything else in
# ``record.__dict__`` is caller-supplied ``extra=`` data, which we merge into the
# JSON payload. Defined explicitly so the contract is obvious. ``taskName``
# exists on Python 3.12+ (this project targets 3.13).
_RESERVED_ATTRS: Final[frozenset[str]] = frozenset(
    {
        "name", "msg", "args", "levelname", "levelno", "pathname", "filename",
        "module", "exc_info", "exc_text", "stack_info", "lineno", "funcName",
        "created", "msecs", "relativeCreated", "thread", "threadName",
        "processName", "process", "taskName", "message", "asctime",
    }
)


class JsonFormatter(logging.Formatter):
    """Emit one JSON object per log record, merging ``extra=`` fields."""

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "timestamp": datetime.fromtimestamp(record.created, tz=UTC).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        for key, value in record.__dict__.items():
            if key not in _RESERVED_ATTRS and not key.startswith("_"):
                payload[key] = value
        if record.exc_info:
            payload["exc_info"] = self.formatException(record.exc_info)
        return json.dumps(payload, default=str, ensure_ascii=False)


class _AppStreamHandler(logging.StreamHandler[TextIO]):
    """Marker subclass so :func:`setup_logging` can detect its own handler."""


def setup_logging(level: str = DEFAULT_LEVEL) -> logging.Logger:
    """Configure the ``app`` logger hierarchy. Idempotent.

    Installs a single JSON :class:`_AppStreamHandler` on the ``app`` logger, sets
    its level from ``level`` (one of DEBUG/INFO/WARNING/ERROR/CRITICAL), and
    disables propagation so records are emitted exactly once — without this,
    records would also flow to the root logger and be double-emitted (in a second
    format) by uvicorn/MCP framework handlers. Safe to call repeatedly.
    """
    logger = logging.getLogger(APP_LOGGER_NAME)
    logger.setLevel(_LEVELS.get(level.upper(), logging.INFO))
    logger.propagate = False
    if not any(isinstance(handler, _AppStreamHandler) for handler in logger.handlers):
        handler = _AppStreamHandler(sys.stderr)
        handler.setFormatter(JsonFormatter())
        logger.addHandler(handler)
    return logger


def get_logger(name: str | None = None) -> logging.Logger:
    """Return a logger in the ``app`` namespace (defaults to ``app``)."""
    return logging.getLogger(APP_LOGGER_NAME if name is None else name)
