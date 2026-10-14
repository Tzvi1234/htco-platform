"""Error monitoring behind a port (SOFTWARE_DESIGN §12, FR-AUD-02).

Code reports unexpected exceptions through `ErrorReporter`. Without a DSN the reporter does
nothing. With one, `SentryErrorReporter` sends the exception and its stack trace, tagged with
the request and trace IDs, and nothing else: no integrations run, local variables are not
captured, and `scrub_event` drops request, user and breadcrumb data as a second line of
defence. The client is isolated: the process-wide Sentry state stays untouched.
"""

from typing import Any, Protocol, Self, cast, override

import sentry_sdk
from sentry_sdk.transport import Transport
from sentry_sdk.types import Event, Hint


class ErrorReporter(Protocol):
    def report(
        self, error: BaseException, *, request_id: str | None, trace_id: str | None
    ) -> None: ...

    def close(self) -> None:
        """Deliver pending reports before the process exits; called once, at shutdown."""
        return


class NoErrorReporter(ErrorReporter):
    """Used when error monitoring is not configured."""

    @override
    def report(self, error: BaseException, *, request_id: str | None, trace_id: str | None) -> None:
        return None


# How long shutdown waits for queued reports to be sent.
_SHUTDOWN_TIMEOUT_SECONDS = 2.0

# Event fields that can hold request bodies, cookies, headers, IP addresses or URLs.
_SCRUBBED_FIELDS = ("request", "user", "breadcrumbs")


def scrub_event(event: Event, _hint: Hint) -> Event:
    """Drop every event field that could carry personal data or secrets."""
    scrubbed = cast(dict[str, Any], dict(event))
    for name in _SCRUBBED_FIELDS:
        scrubbed.pop(name, None)
    return cast(Event, scrubbed)


class SentryErrorReporter(ErrorReporter):
    def __init__(self, client: sentry_sdk.Client) -> None:
        self._client = client

    @classmethod
    def from_dsn(
        cls,
        dsn: str,
        *,
        environment: str,
        release: str | None,
        transport: Transport | None = None,
    ) -> Self:
        client = sentry_sdk.Client(
            dsn=dsn,
            environment=environment,
            release=release,
            transport=transport,
            default_integrations=False,
            auto_enabling_integrations=False,
            send_default_pii=False,
            include_local_variables=False,
            max_request_body_size="never",
            before_send=scrub_event,
        )
        return cls(client)

    @override
    def report(self, error: BaseException, *, request_id: str | None, trace_id: str | None) -> None:
        with sentry_sdk.isolation_scope() as scope:
            scope.set_client(self._client)
            if request_id is not None:
                scope.set_tag("request_id", request_id)
            if trace_id is not None:
                scope.set_tag("trace_id", trace_id)
            sentry_sdk.capture_exception(error)

    def flush(self, timeout_seconds: float = 2.0) -> None:
        self._client.flush(timeout=timeout_seconds)

    @override
    def close(self) -> None:
        # The transport sends from a background worker; without this, reports queued just
        # before a deploy or restart would be lost.
        self._client.close(timeout=_SHUTDOWN_TIMEOUT_SECONDS)
