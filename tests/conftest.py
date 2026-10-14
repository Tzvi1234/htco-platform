"""Shared pytest configuration for the HTCO platform test suite."""

import pytest


@pytest.fixture
def anyio_backend() -> str:
    """Run `@pytest.mark.anyio` tests on asyncio only, the event loop used in production."""
    return "asyncio"
