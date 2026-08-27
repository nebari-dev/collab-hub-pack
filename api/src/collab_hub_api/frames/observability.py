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


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload = {
            "level": record.levelname.lower(),
            "logger": record.name,
            "message": record.getMessage(),
        }
        for key in (
            "request_id",
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
            route_path = getattr(route, "path", request.url.path)
            REQUEST_COUNT.labels(
                method=request.method,
                path=route_path,
                status=str(status_code),
            ).inc()
            REQUEST_DURATION.labels(
                method=request.method,
                path=route_path,
            ).observe(duration)
            access_logger.info(
                "request",
                extra={
                    "request_id": request_id,
                    "method": request.method,
                    "path": route_path,
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
