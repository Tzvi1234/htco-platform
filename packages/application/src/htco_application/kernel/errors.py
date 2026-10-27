"""Errors whose message is safe to store."""

MAX_ERROR_LENGTH = 500


class JobError(Exception):
    """A job failure whose message is safe to store and show to admins: no buyer data, no
    secrets. Any other exception is stored by its type name only."""


class DeadLetterError(JobError):
    """A failure that retrying cannot fix: the outbox message goes to the dead letters at once."""


def describe_error(error: BaseException) -> str:
    """The `last_error` kept with a failed job."""
    name = type(error).__name__
    if isinstance(error, JobError) and str(error):
        return f"{name}: {error}"[:MAX_ERROR_LENGTH]
    return name[:MAX_ERROR_LENGTH]
