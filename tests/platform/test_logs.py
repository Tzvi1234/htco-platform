"""Every log line is one JSON object with timestamp, level, event and request_id (AC-1.2.6)."""

import json
import logging
from datetime import datetime

import pytest
import structlog

from tests.log_capture import LogCapture


@pytest.mark.ac("AC-1.2.6")
def test_structlog_line__outside_a_request__has_the_required_fields(logs: LogCapture) -> None:
    structlog.get_logger("htco.test").info("api.starting", environment="test")

    [line] = logs.lines()
    assert line["event"] == "api.starting"
    assert line["level"] == "info"
    assert line["request_id"] is None
    assert line["environment"] == "test"
    assert datetime.fromisoformat(line["timestamp"]).utcoffset() is not None


@pytest.mark.ac("AC-1.2.6")
def test_standard_library_line__from_a_library__is_json_with_the_required_fields(
    logs: LogCapture,
) -> None:
    logging.getLogger("uvicorn.error").warning("Started server process [%d]", 42)

    [line] = logs.lines()
    assert line["event"] == "Started server process [42]"
    assert line["level"] == "warning"
    assert line["logger"] == "uvicorn.error"
    assert "timestamp" in line
    assert line["request_id"] is None


@pytest.mark.ac("AC-1.2.6")
def test_bound_request_id__appears_in_structlog_and_library_lines(logs: LogCapture) -> None:
    with structlog.contextvars.bound_contextvars(request_id="0197-req"):
        structlog.get_logger("htco.test").info("work.done")
        logging.getLogger("uvicorn.error").info("Waiting for application startup.")

    assert [line["request_id"] for line in logs.lines()] == ["0197-req", "0197-req"]


def test_exception__is_logged_with_a_structured_stack_trace(logs: LogCapture) -> None:
    try:
        raise ValueError("boom")  # noqa: TRY301
    except ValueError:
        structlog.get_logger("htco.test").exception("request.failed")

    [line] = logs.lines()
    [frame_group] = line["exception"]
    assert frame_group["exc_type"] == "ValueError"
    assert frame_group["frames"]


@pytest.mark.parametrize("library", ["httpx", "httpcore"])
def test_http_client_request_lines__are_not_logged(logs: LogCapture, library: str) -> None:
    """httpx logs every URL, query string included, at INFO; query strings can hold tokens."""
    logging.getLogger(library).info("HTTP Request: GET https://api.example/v3?api_key=k-123")

    assert "k-123" not in logs.raw()


def test_http_client_warnings__are_still_logged(logs: LogCapture) -> None:
    logging.getLogger("httpx").warning("connection pool is full")

    assert [line["event"] for line in logs.lines()] == ["connection pool is full"]


def test_level__debug_lines__are_dropped_at_info(logs: LogCapture) -> None:
    structlog.get_logger("htco.test").debug("noise")
    logging.getLogger("uvicorn.error").debug("more noise")

    assert logs.lines() == []


def test_every_line__is_valid_json_on_a_single_line(logs: LogCapture) -> None:
    structlog.get_logger("htco.test").info("multi\nline", detail="a\nb")

    raw = logs.raw()
    assert raw.count("\n") == 1
    assert json.loads(raw)["detail"] == "a\nb"
