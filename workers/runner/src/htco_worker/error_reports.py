"""Dead-lettered executions reach error monitoring (AC-6.7.4).

The engine calls `on_dead_letter` with a `DeadLetter` once the dead letter is stored. Here that
becomes a report to the worker's `ErrorReporter`: Sentry when `HTCO_SENTRY_DSN` is set, nothing
otherwise. The report says only what the `DeadLetter` holds (the automation, the execution and
the stored error), so no buyer name, email or address can reach it. The automation, the error
type and the execution are also tags, to filter by; each dead letter stays its own Sentry issue
(no fingerprint), so the "new issue" alert fires for every one (AC-6.7.4: never silently).
"""

from htco_application.automation.durable import DeadLetter, DeadLetterReporter
from htco_platform.error_reporting import ErrorReporter, NoErrorReporter, SentryErrorReporter
from htco_worker.settings import WorkerSettings


class DeadLetteredExecutionError(Exception):
    """An automation execution that ran out of retries, as error monitoring sees it. Raised by
    HTCO's own code, so `scrub_event` keeps its message, which holds identifiers only."""

    def __init__(self, dead: DeadLetter) -> None:
        super().__init__(
            f"automation.dead_lettered: {dead.automation_id} execution={dead.execution_id} "
            f"error_type={dead.error_type} error={dead.error}"
        )


def dead_letter_reporter(reporter: ErrorReporter) -> DeadLetterReporter:
    def report(dead: DeadLetter) -> None:
        reporter.report(
            DeadLetteredExecutionError(dead),
            request_id=None,
            trace_id=None,
            tags={
                "automation_id": dead.automation_id,
                "error_type": dead.error_type,
                "execution_id": str(dead.execution_id),
            },
        )

    return report


def worker_error_reporter(settings: WorkerSettings) -> ErrorReporter:
    """Sentry when the DSN is set, otherwise a reporter that does nothing."""
    if settings.sentry_dsn is None:
        return NoErrorReporter()
    return SentryErrorReporter.from_dsn(
        settings.sentry_dsn.get_secret_value(),
        environment=settings.environment,
        release=settings.release,
    )
