"""Shared parameter validation and envelope helpers for MCP tools.

Validation messages echo caller input only (never URLs, status codes, or
credentials), so they are safe to surface to the model. Tools call
``page_bounds_detail`` / ``time_window_detail`` before touching the SDNClient,
and use ``skeleton_payload`` / ``unexpected_payload`` for the two standard
non-success envelopes every tool returns.
"""

from __future__ import annotations

from datetime import datetime

from app.sdn.client import PERF_TIME_FORMAT

MAX_PAGE_SIZE = 100

SKELETON_DETAIL = "SDN controller not configured (skeleton mode)."
GRAPH_SKELETON_DETAIL = "SOP graph not configured (skeleton mode)."
UNEXPECTED_DETAIL = "Unexpected server error."
NOT_IMPLEMENTED_DETAIL = "Tool is registered but not implemented yet."


def page_bounds_detail(page_num: int, page_size: int) -> str | None:
    """Return a safe error detail if page bounds are invalid, else None."""
    if page_num < 1:
        return f"page_num must be >= 1 (got {page_num})"
    if not 1 <= page_size <= MAX_PAGE_SIZE:
        return f"page_size must be in [1, {MAX_PAGE_SIZE}] (got {page_size})"
    return None


def positive_bound_detail(name: str, value: int, maximum: int) -> str | None:
    """Return a safe error detail if ``value`` is outside [1, maximum], else None."""
    if not 1 <= value <= maximum:
        return f"{name} must be in [1, {maximum}] (got {value})"
    return None


def time_window_detail(start_time: str | None, end_time: str | None) -> str | None:
    """Return a safe error detail if the time window is invalid, else None.

    Both endpoints must be given together (or neither, to use the default
    last-hour window) and must follow the controller's time format with
    start <= end.
    """
    if (start_time is None) != (end_time is None):
        return "start_time and end_time must be provided together"
    if start_time is None or end_time is None:
        return None
    try:
        start = datetime.strptime(start_time, PERF_TIME_FORMAT)
        end = datetime.strptime(end_time, PERF_TIME_FORMAT)
    except ValueError:
        return f"start_time/end_time must follow '{PERF_TIME_FORMAT}' (e.g. '2026-07-29 10:00:00')"
    if start > end:
        return "start_time must not be later than end_time"
    return None


def skeleton_payload() -> dict[str, object]:
    """Envelope returned when the controller is not configured (skeleton mode)."""
    return {"ok": False, "configured": False, "detail": SKELETON_DETAIL}


def graph_skeleton_payload() -> dict[str, object]:
    """Envelope returned when the SOP graph is not configured (skeleton mode)."""
    return {"ok": False, "configured": False, "detail": GRAPH_SKELETON_DETAIL}


def unexpected_payload() -> dict[str, object]:
    """Envelope returned for any non-SDNError failure (catch-all, no leak)."""
    return {"ok": False, "configured": False, "detail": UNEXPECTED_DETAIL}


def not_implemented_payload(hint: str | None = None) -> dict[str, object]:
    """Envelope returned by a registered-but-unimplemented tool.

    ``configured`` is False because no backing data source is wired yet. The
    optional ``hint`` names the pending data source so the agent can explain the
    gap instead of retrying.
    """
    payload: dict[str, object] = {
        "ok": False,
        "configured": False,
        "detail": NOT_IMPLEMENTED_DETAIL,
    }
    if hint:
        payload["hint"] = hint
    return payload
