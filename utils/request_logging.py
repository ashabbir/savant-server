"""HTTP request lifecycle logging middleware for Flask.

Attaches a unique request ID and start timer to every request, then emits
a single structured log line when the response is sent.  The log line
includes method, path, status code, authenticated user, source application,
and wall-clock duration in milliseconds.

Usage::

    from utils.request_logging import install_request_logging
    install_request_logging(app)
"""

import logging
import time
import uuid

from flask import Flask, g, request

logger = logging.getLogger("savant.http")

# Paths that generate high-frequency noise and no diagnostic value.
_QUIET_PREFIXES = ("/health", "/static/", "/favicon.ico")


def _should_log(path: str) -> bool:
    """Return False for health-check and static-asset paths."""
    for prefix in _QUIET_PREFIXES:
        if path.startswith(prefix):
            return False
    return True


def _before_request():
    """Stamp each request with a unique ID and start timer."""
    g.request_id = request.headers.get("X-Request-Id") or uuid.uuid4().hex[:12]
    g.request_start = time.perf_counter()


def _after_request(response):
    """Emit a single structured log line for each completed request."""
    path = request.path or "/"
    if not _should_log(path):
        return response

    duration_ms = (time.perf_counter() - getattr(g, "request_start", 0)) * 1000
    user = getattr(g, "user_id", "-") or "-"
    app_name = (
        request.headers.get("X-App-Name")
        or request.headers.get("X-Savant-App")
        or "-"
    ).strip()

    logger.info(
        "%s %s → %d | user=%s app=%s req=%s %.0fms",
        request.method,
        path,
        response.status_code,
        user,
        app_name,
        getattr(g, "request_id", "-"),
        duration_ms,
    )
    response.headers["X-Request-Id"] = getattr(g, "request_id", "")
    return response


def install_request_logging(app: Flask) -> None:
    """Register before/after hooks on the Flask app."""
    app.before_request(_before_request)
    app.after_request(_after_request)
