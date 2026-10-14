"""Shared pytest configuration for the HTCO platform test suite."""

import os

import pytest

from tests.persistence.database import DATABASE_URL_VARIABLE, REQUIRE_VARIABLE


@pytest.fixture
def anyio_backend() -> str:
    """Run `@pytest.mark.anyio` tests on asyncio only, the event loop used in production."""
    return "asyncio"


def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:
    """Database tests need a running Postgres (`just db-start`).

    Without `HTCO_TEST_DATABASE_URL` they are deselected and reported, so `check-fast` stays
    usable on a machine without Docker. CI sets `HTCO_REQUIRE_DATABASE_TESTS=1`, which turns a
    missing database into a failure: CI always runs them (ER-TEST-07).
    """
    if os.environ.get(DATABASE_URL_VARIABLE):
        return
    needs_database = [item for item in items if item.get_closest_marker("database")]
    if not needs_database:
        return
    if os.environ.get(REQUIRE_VARIABLE) == "1":
        raise pytest.UsageError(f"{REQUIRE_VARIABLE}=1 but {DATABASE_URL_VARIABLE} is not set")
    config.hook.pytest_deselected(items=needs_database)
    items[:] = [item for item in items if not item.get_closest_marker("database")]
    reporter = config.pluginmanager.get_plugin("terminalreporter")
    if reporter is not None:
        reporter.write_line(
            f"{len(needs_database)} database test(s) deselected: set {DATABASE_URL_VARIABLE} "
            "(`just db-start` prints it) to run them"
        )
