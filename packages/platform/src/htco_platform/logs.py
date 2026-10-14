"""Structured JSON logging for every HTCO process (SOFTWARE_DESIGN §12, ASVS 16.2).

Each line is one JSON object with `timestamp` (ISO 8601, UTC), `level`, `event`, `logger`,
`request_id` (null outside a request) and `trace_id` (null outside a trace; `span_id` joins it
inside one), plus any bound context. Libraries that log through the
standard `logging` module (uvicorn, httpx) are rendered the same way.
"""

import logging
import sys
from typing import TextIO

import structlog
from structlog.typing import EventDict, Processor, WrappedLogger

from htco_platform.tracing import current_trace_ids

# Library loggers that install their own handlers; ours replace them.
_LIBRARY_LOGGERS = ("uvicorn", "uvicorn.error", "uvicorn.access")
# HTTP clients log every request URL, query string included, at INFO. Query strings can carry
# tokens and API keys, so these loggers only report warnings and errors (ASVS 16.2.5).
_QUIET_HTTP_CLIENT_LOGGERS = ("httpx", "httpcore")


def _ensure_request_id(_logger: WrappedLogger, _method: str, event_dict: EventDict) -> EventDict:
    event_dict.setdefault("request_id", None)
    return event_dict


def _add_trace_context(_logger: WrappedLogger, _method: str, event_dict: EventDict) -> EventDict:
    ids = current_trace_ids()
    if ids is None:
        event_dict.setdefault("trace_id", None)
    else:
        event_dict["trace_id"], event_dict["span_id"] = ids
    return event_dict


def configure_logging(*, level: str, stream: TextIO | None = None) -> None:
    """Send every log line, ours and the libraries', to `stream` (stdout by default) as JSON."""
    shared: list[Processor] = [
        structlog.contextvars.merge_contextvars,
        structlog.stdlib.add_log_level,
        structlog.stdlib.add_logger_name,
        structlog.stdlib.PositionalArgumentsFormatter(),
        structlog.processors.TimeStamper(fmt="iso", utc=True, key="timestamp"),
        _ensure_request_id,
        _add_trace_context,
    ]
    structlog.configure(
        processors=[
            structlog.stdlib.filter_by_level,
            *shared,
            structlog.stdlib.ProcessorFormatter.wrap_for_formatter,
        ],
        logger_factory=structlog.stdlib.LoggerFactory(),
        wrapper_class=structlog.stdlib.BoundLogger,
        cache_logger_on_first_use=True,
    )
    formatter = structlog.stdlib.ProcessorFormatter(
        foreign_pre_chain=shared,
        processors=[
            structlog.stdlib.ProcessorFormatter.remove_processors_meta,
            structlog.processors.dict_tracebacks,
            structlog.processors.JSONRenderer(),
        ],
    )
    handler = logging.StreamHandler(stream or sys.stdout)
    handler.setFormatter(formatter)
    root = logging.getLogger()
    root.handlers[:] = [handler]
    root.setLevel(level)
    for name in _LIBRARY_LOGGERS:
        library_logger = logging.getLogger(name)
        library_logger.handlers.clear()
        library_logger.propagate = True
    for name in _QUIET_HTTP_CLIENT_LOGGERS:
        logging.getLogger(name).setLevel(logging.WARNING)
