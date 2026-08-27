from __future__ import annotations

import json
import logging
import sys
import time
from collections.abc import Callable
from uuid import uuid4

from fastapi import Request, Response
from prometheus_client import CONTENT_TYPE_LATEST, Counter, Histogram, generate_latest
from starlette.middleware.base import BaseHTTPMiddleware

REQUEST_COUNT = Counter(
    "frames_server_http_requests_total",
    "Total HTTP requests.",
    ["method", "path", "status"],
)
REQUEST_DURATION = Histogram(
    "frames_server_http_request_duration_seconds",
    "HTTP request duration in seconds.",
    ["method", "path"],
)
AUDIT_EVENTS = Counter(
    "frames_server_audit_events_total",
    "Frame mutation audit events.",
    ["action"],
)
HISTORY_WRITE_FAILURES = Counter(
    "frames_server_history_write_failures_total",
    "Frame history rows dropped because the persistent write failed.",
    ["event"],
)
USAGE_EVENTS = Counter(
    "frames_server_usage_events_total",
    "Client-reported usage events recorded.",
    ["event"],
)
USAGE_WRITE_FAILURES = Counter(
    "frames_server_usage_write_failures_total",
    "Usage rows dropped because the persistent write failed.",
    ["kind"],
)


UNMATCHED_PATH_LABEL = "<unmatched>"
"""The ``path`` metric label for a request that never reached a route.

Prometheus label values must come from a bounded set. A request path does not
belong to one — it is chosen by the caller — so anything answered before
routing (pre-routing credential refusals, the browser surface's sign-in
redirects and socket refusals) is counted under this single sentinel instead.

It cannot collide with a real route template because Starlette requires a
route path to start with ``/`` (``Route.__init__`` asserts it) and this value
does not. Not because of the angle brackets: ``Route("/<unmatched>", ...)`` is
perfectly legal, and an earlier version of this comment claimed otherwise.
"""

KNOWN_METHODS = frozenset(
    {
        "GET",
        "HEAD",
        "POST",
        "PUT",
        "PATCH",
        "DELETE",
        "OPTIONS",
        "TRACE",
        "CONNECT",
        # The pseudo-method the browser surface records for a refused
        # WebSocket handshake, which has no HTTP method of its own.
        "WEBSOCKET",
    }
)

OTHER_METHOD_LABEL = "OTHER"
"""The ``method`` metric label for anything outside :data:`KNOWN_METHODS`.

The same unbounded-cardinality hole as the path label, one axis over, and it
stayed open after the path was closed. HTTP allows arbitrary extension tokens
as methods and the parser accepts them, so ``method=request.method`` let an
unauthenticated caller mint a fresh series per invented verb — ``X0``,
``X1``, … — exactly as an invented path once did.
"""


def metric_method(method: str) -> str:
    """Bound a request method to the fixed label set."""

    return method if method in KNOWN_METHODS else OTHER_METHOD_LABEL


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload = {
            "level": record.levelname.lower(),
            "logger": record.name,
            "message": record.getMessage(),
        }
        for key in (
            "request_id",
            # Recorded separately from request_id, and named so that nothing
            # reading these lines can mistake a caller-supplied value for the
            # server's own correlation id.
            "client_request_id",
            "method",
            "path",
            "status_code",
            "duration_ms",
            "user",
            "action",
            "frame_id",
            "suggestion_id",
        ):
            value = getattr(record, key, None)
            if value is not None:
                payload[key] = value
        return json.dumps(payload, separators=(",", ":"))


def configure_logging() -> None:
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(JsonFormatter())
    root = logging.getLogger()
    if not root.handlers:
        root.handlers = [handler]
    root.setLevel(logging.INFO)


access_logger = logging.getLogger("frames_server.access")
audit_logger = logging.getLogger("frames_server.audit")


class RequestObservabilityMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next: Callable) -> Response:
        request_id = request.headers.get("x-request-id", uuid4().hex)
        request.state.request_id = request_id
        start = time.perf_counter()
        status_code = 500
        try:
            response = await call_next(request)
            status_code = response.status_code
            response.headers["x-request-id"] = request_id
            return response
        finally:
            duration = time.perf_counter() - start
            route = request.scope.get("route")
            route_path = getattr(route, "path", None)
            # Metric labels come from the matched route *template* only. A
            # request answered before routing — a credential refusal from the
            # path-protection middleware, a browser-surface sign-in redirect —
            # has no template, and using its raw URL path there let an
            # unauthenticated client mint one Prometheus series per path it
            # invented: unbounded cardinality, which is memory exhaustion of
            # the metrics store by anyone who can reach the port.
            metric_path = route_path if route_path is not None else UNMATCHED_PATH_LABEL
            # Both label axes are bounded, for the same reason: every value in
            # a Prometheus label must come from a fixed set, and the caller
            # chooses the method just as freely as the path.
            method_label = metric_method(request.method)
            REQUEST_COUNT.labels(
                method=method_label,
                path=metric_path,
                status=str(status_code),
            ).inc()
            REQUEST_DURATION.labels(
                method=method_label,
                path=metric_path,
            ).observe(duration)
            access_logger.info(
                "request",
                extra={
                    "request_id": request_id,
                    # The log keeps the real path: it is bounded by retention
                    # rather than held in memory forever, and "which path was
                    # refused" is the question an operator actually has.
                    "path": route_path if route_path is not None else request.url.path,
                    "method": request.method,
                    "status_code": status_code,
                    "duration_ms": round(duration * 1000, 2),
                },
            )


def metrics_response() -> Response:
    return Response(generate_latest(), media_type=CONTENT_TYPE_LATEST)


def audit_event(
    action: str,
    request: Request,
    user: str,
    frame_id: str | None = None,
    suggestion_id: str | None = None,
) -> None:
    AUDIT_EVENTS.labels(action=action).inc()
    audit_logger.info(
        "audit",
        extra={
            "request_id": getattr(request.state, "request_id", None),
            "action": action,
            "user": user,
            "frame_id": frame_id,
            "suggestion_id": suggestion_id,
        },
    )
