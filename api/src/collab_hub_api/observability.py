from __future__ import annotations

from typing import TYPE_CHECKING

import anyio
import fastapi
import l2sl
import starlette
import structlog
import uvicorn

if TYPE_CHECKING:
    from structlog.typing import EventDict, WrappedLogger

    from collab_hub_api.config import LoggingConfig


def _drop_health_probe_access_logs(logger: WrappedLogger, method_name: str, event_dict: EventDict) -> EventDict:
    if event_dict.get("logger") == "uvicorn.access" and event_dict["endpoint"] == "/health":
        raise structlog.DropEvent()

    return event_dict


def configure_logging(config: LoggingConfig) -> None:
    suppress_locals = [anyio, fastapi, starlette, uvicorn]

    structlog.configure(
        cache_logger_on_first_use=True,
        wrapper_class=structlog.make_filtering_bound_logger(config.level.structlog_name),
        processors=[
            *(
                [
                    _drop_health_probe_access_logs,
                ]
                if config.level > "debug"
                else [
                    structlog.processors.CallsiteParameterAdder(additional_ignores=["l2sl"]),
                ]
            ),
            structlog.contextvars.merge_contextvars,
            structlog.processors.add_log_level,
            structlog.processors.TimeStamper(fmt="iso", utc=True),
            structlog.dev.set_exc_info,
            *(
                [  # type: ignore[list-item]
                    structlog.processors.ExceptionRenderer(
                        structlog.processors.ExceptionDictTransformer(suppress=suppress_locals)
                    ),
                    structlog.processors.JSONRenderer(),
                ]
                if config.as_json
                else [
                    structlog.dev.ConsoleRenderer(
                        exception_formatter=structlog.dev.RichTracebackFormatter(suppress=suppress_locals)
                    ),
                ]
            ),
        ],
    )

    l2sl.configure_stdlib_log_forwarding()
