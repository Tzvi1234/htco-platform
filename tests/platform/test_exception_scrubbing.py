"""Exception messages never reach logs or Sentry unscrubbed (audit P1-5, slice 5.9).

A database error's DETAIL line can quote a buyer's row (`Key (buyer_name)=(…) already exists`).
Only messages that HTCO's own code wrote are kept; every other exception shows as its type, and
a database error also by its SQLSTATE."""

from typing import Any, cast

import structlog
from sentry_sdk.types import Event, Hint

from htco_platform.error_reporting import SentryErrorReporter, scrub_event
from htco_platform.scrubbing import safe_exception_text
from tests.log_capture import LogCapture
from tests.sentry_capture import FAKE_DSN, CapturingTransport

BUYER = "Fakebuyer Quuxley, 12 Fake Street"


class UniqueViolation(Exception):  # noqa: N818 - named like the driver's error
    """Shaped like psycopg's: a SQLSTATE and a message that quotes the row."""

    sqlstate = "23505"


class HtcoLikeError(Exception):
    """An exception of HTCO's own code: its message is written by us, never with buyer data."""


HtcoLikeError.__module__ = "htco_application.orders"


def _database_error() -> UniqueViolation:
    return UniqueViolation(f"duplicate key value\nDETAIL:  Key (buyer_name)=({BUYER}) exists.")


# --- the rule ---------------------------------------------------------------------------------


def test_safe_text__a_database_error__type_and_sqlstate_only() -> None:
    assert safe_exception_text(_database_error()) == "UniqueViolation (SQLSTATE 23505)"


def test_safe_text__a_library_error__its_type_only() -> None:
    assert safe_exception_text(ValueError(f"bad value {BUYER}")) == "ValueError"


def test_safe_text__htcos_own_error__keeps_its_message() -> None:
    error = HtcoLikeError("Etsy answered 503 to list_receipts")

    assert safe_exception_text(error) == "Etsy answered 503 to list_receipts"


# --- logs -------------------------------------------------------------------------------------


def test_log__an_exception_message_with_buyer_data__never_in_the_line(logs: LogCapture) -> None:
    try:
        raise _database_error()
    except UniqueViolation:
        structlog.get_logger("htco.test").exception("request.failed")

    [line] = logs.lines()
    assert BUYER not in logs.raw()
    [stack] = line["exception"]
    assert stack["exc_type"] == "UniqueViolation"
    assert stack["exc_value"] == "UniqueViolation (SQLSTATE 23505)"
    assert stack["frames"]


def test_log__a_chained_cause__scrubbed_too(logs: LogCapture) -> None:
    try:
        try:
            raise _database_error()
        except UniqueViolation as cause:
            raise HtcoLikeError("the order could not be stored") from cause
    except HtcoLikeError:
        structlog.get_logger("htco.test").exception("orders.store_failed")

    assert BUYER not in logs.raw()
    values = [stack["exc_value"] for stack in logs.lines()[0]["exception"]]
    assert "the order could not be stored" in values
    assert "UniqueViolation (SQLSTATE 23505)" in values


# --- Sentry -----------------------------------------------------------------------------------


def test_scrub_event__replaces_every_exception_value() -> None:
    error = _database_error()
    event = cast(
        Event,
        {"exception": {"values": [{"type": "UniqueViolation", "value": str(error)}]}},
    )

    scrubbed = cast(
        dict[str, Any], scrub_event(event, cast(Hint, {"exc_info": (type(error), error, None)}))
    )

    assert scrubbed["exception"]["values"][0]["value"] == "UniqueViolation (SQLSTATE 23505)"


def test_scrub_event__without_the_exception__no_value_survives() -> None:
    event = cast(Event, {"exception": {"values": [{"type": "KeyError", "value": BUYER}]}})

    scrubbed = cast(dict[str, Any], scrub_event(event, cast(Hint, {})))

    assert BUYER not in str(scrubbed)
    assert scrubbed["exception"]["values"][0]["value"] == "KeyError"


def test_sentry_reporter__a_database_error__sent_without_its_message() -> None:
    transport = CapturingTransport()
    reporter = SentryErrorReporter.from_dsn(FAKE_DSN, environment="test", release=None,
                                            transport=transport)  # fmt: skip

    try:
        try:
            raise _database_error()
        except UniqueViolation as cause:
            raise HtcoLikeError("the order could not be stored") from cause
    except HtcoLikeError as error:
        reporter.report(error, request_id=None, trace_id=None)
    reporter.flush()

    [event] = transport.events
    assert BUYER not in str(event)
    values = [v["value"] for v in event["exception"]["values"]]
    assert "UniqueViolation (SQLSTATE 23505)" in values
    assert "the order could not be stored" in values
