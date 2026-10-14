"""Capture the JSON log lines written through `configure_logging` during one test."""

import io
import json
import logging
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

import structlog

from htco_platform.logs import configure_logging


class LogCapture:
    def __init__(self, stream: io.StringIO) -> None:
        self._stream = stream

    def raw(self) -> str:
        return self._stream.getvalue()

    def lines(self) -> list[dict[str, Any]]:
        return [json.loads(line) for line in self.raw().splitlines() if line.strip()]


@contextmanager
def capture_logs(level: str = "INFO") -> Iterator[LogCapture]:
    """Route all logging to a buffer at `level`, then restore the previous root handlers."""
    root = logging.getLogger()
    saved_handlers, saved_level = root.handlers[:], root.level
    stream = io.StringIO()
    configure_logging(level=level, stream=stream)
    try:
        yield LogCapture(stream)
    finally:
        structlog.contextvars.clear_contextvars()
        root.handlers[:] = saved_handlers
        root.setLevel(saved_level)
