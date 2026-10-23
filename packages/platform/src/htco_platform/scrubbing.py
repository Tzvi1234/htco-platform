"""What an exception may say in logs and error reports (audit P1-5, slice 5.9).

A library's message is not ours to show: a database error's DETAIL line can quote a buyer's
row, and an HTTP error can quote a URL with a token in it. So only exceptions raised by HTCO's
own packages (`htco_*`, whose messages the code writes without buyer data or secrets) keep their
message. Every other exception is shown as its type, and a database error also by its SQLSTATE.
The structlog processors and Sentry's `before_send` both use `safe_exception_text`.
"""

import sys
from collections.abc import Iterator

from structlog.typing import EventDict, WrappedLogger

_OWN_PACKAGE_PREFIX = "htco_"
REDACTED = "<redacted>"
# The key the pre-processor leaves for the post-processor: original message → safe text.
_SAFE_TEXTS = "_htco_safe_exception_texts"


def safe_exception_text(error: BaseException) -> str:
    """The message when HTCO's own code wrote it; otherwise the type (and SQLSTATE)."""
    kind = type(error)
    if kind.__module__.split(".", 1)[0].startswith(_OWN_PACKAGE_PREFIX):
        return str(error)
    sqlstate = getattr(error, "sqlstate", None)
    if isinstance(sqlstate, str) and sqlstate:
        return f"{kind.__name__} (SQLSTATE {sqlstate})"
    return kind.__name__


def exception_chain(error: BaseException) -> Iterator[BaseException]:
    """The exception, its causes and contexts, each once."""
    seen: set[int] = set()
    current: BaseException | None = error
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        yield current
        current = current.__cause__ or current.__context__


def safe_texts(error: BaseException) -> dict[str, str]:
    """Each message of the chain mapped to what may be shown instead."""
    return {str(e): safe_exception_text(e) for e in exception_chain(error)}


def _exception_of(event_dict: EventDict) -> BaseException | None:
    exc_info = event_dict.get("exc_info")
    if isinstance(exc_info, BaseException):
        return exc_info
    if isinstance(exc_info, tuple) and len(exc_info) == 3:  # noqa: PLR2004 - sys.exc_info()
        found = exc_info[1]
        return found if isinstance(found, BaseException) else None
    if exc_info is True:
        return sys.exc_info()[1]
    return None


def remember_safe_texts(_logger: WrappedLogger, _method: str, event_dict: EventDict) -> EventDict:
    """Before the traceback is rendered: note what each message of the chain may say."""
    error = _exception_of(event_dict)
    if error is not None:
        event_dict[_SAFE_TEXTS] = safe_texts(error)
    return event_dict


def scrub_rendered_exceptions(
    _logger: WrappedLogger, _method: str, event_dict: EventDict
) -> EventDict:
    """After `dict_tracebacks`: replace every rendered message by its safe text. A message the
    pre-processor did not see (rendered elsewhere) is redacted."""
    texts: dict[str, str] = event_dict.pop(_SAFE_TEXTS, {})
    stacks = event_dict.get("exception")
    if isinstance(stacks, list):
        for stack in stacks:
            if isinstance(stack, dict) and "exc_value" in stack:
                stack["exc_value"] = texts.get(str(stack["exc_value"]), REDACTED)
    return event_dict
