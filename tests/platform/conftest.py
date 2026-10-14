"""Fixtures for the platform package tests."""

from collections.abc import Iterator

import pytest

from tests.log_capture import LogCapture, capture_logs


@pytest.fixture
def logs() -> Iterator[LogCapture]:
    """JSON logging at INFO into a buffer for the duration of one test."""
    with capture_logs() as captured:
        yield captured
