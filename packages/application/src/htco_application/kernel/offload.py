"""Running blocking store calls from async use cases."""

from collections.abc import Awaitable, Callable
from typing import Protocol


class Offload(Protocol):
    """Runs a blocking call off the event loop (`anyio.to_thread.run_sync` in production)."""

    def __call__[T](self, work: Callable[[], T], /) -> Awaitable[T]: ...


async def run_inline[T](work: Callable[[], T]) -> T:
    """Runs store calls in the event loop (tests and in-memory stores)."""
    return work()
