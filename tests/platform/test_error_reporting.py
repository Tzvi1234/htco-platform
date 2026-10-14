"""Error monitoring through a port; the Sentry adapter sends exceptions, never request data."""

from typing import cast

import sentry_sdk
from sentry_sdk.types import Event

from htco_platform.error_reporting import NoErrorReporter, SentryErrorReporter, scrub_event
from tests.sentry_capture import FAKE_DSN, CapturingTransport


def _explode() -> None:
    password = "".join(["hun", "ter2"])  # a runtime value, absent from the source text
    raise RuntimeError(f"diagnostic error ({len(password)})")


def _reporter(transport: CapturingTransport) -> SentryErrorReporter:
    return SentryErrorReporter.from_dsn(
        FAKE_DSN, environment="test", release="abc123", transport=transport
    )


def test_sentry_reporter__exception__is_sent_with_request_and_trace_ids() -> None:
    transport = CapturingTransport()
    reporter = _reporter(transport)

    try:
        _explode()
    except RuntimeError as error:
        reporter.report(error, request_id="req-1", trace_id="0af7651916cd43dd8448eb211c80319c")
    reporter.flush()

    [event] = transport.events
    assert event["tags"] == {"request_id": "req-1", "trace_id": "0af7651916cd43dd8448eb211c80319c"}
    assert event["environment"] == "test"
    assert event["release"] == "abc123"
    assert event["exception"]["values"][0]["type"] == "RuntimeError"


def test_sentry_reporter__close__delivers_pending_reports() -> None:
    transport = CapturingTransport()
    reporter = _reporter(transport)

    reporter.report(RuntimeError("at shutdown"), request_id=None, trace_id=None)
    reporter.close()

    assert len(transport.events) == 1


def test_sentry_reporter__frames__carry_no_local_variables() -> None:
    transport = CapturingTransport()
    reporter = _reporter(transport)

    try:
        _explode()
    except RuntimeError as error:
        reporter.report(error, request_id=None, trace_id=None)
    reporter.flush()

    frames = transport.events[0]["exception"]["values"][0]["stacktrace"]["frames"]
    assert all("vars" not in frame for frame in frames)
    assert "hunter2" not in transport.serialized()
    assert transport.events[0].get("tags", {}) == {}


def test_sentry_reporter__leaves_the_global_sentry_client_untouched() -> None:
    reporter = _reporter(CapturingTransport())

    try:
        _explode()
    except RuntimeError as error:
        reporter.report(error, request_id="req-1", trace_id=None)

    assert not sentry_sdk.get_client().is_active()


def test_scrub_event__removes_request_user_and_breadcrumbs() -> None:
    event = {  # an event as integrations would build it, beyond Sentry's typed fields
        "message": "boom",
        "request": {"cookies": "session=secret", "data": "card=4111"},
        "user": {"ip_address": "203.0.113.7"},
        "breadcrumbs": {"values": [{"message": "GET /?token=secret"}]},
    }

    assert scrub_event(cast(Event, event), {}) == {"message": "boom"}


def test_no_error_reporter__close__does_nothing() -> None:
    NoErrorReporter().close()


def test_no_error_reporter__accepts_reports_and_does_nothing() -> None:
    NoErrorReporter().report(RuntimeError("x"), request_id="req-1", trace_id=None)
