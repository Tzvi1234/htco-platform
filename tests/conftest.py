"""Shared pytest configuration for the HTCO platform test suite."""

import pytest

from tests.sharding import is_kept, parse_shard


def pytest_addoption(parser: pytest.Parser) -> None:
    parser.addoption(
        "--fuzz-shard",
        default=None,
        metavar="INDEX/COUNT",
        help=(
            "Run only this shard of the API fuzzing tests (a stable crc32 of the node id), "
            "e.g. 0/4; other tests are never deselected."
        ),
    )


def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:
    spec = config.getoption("--fuzz-shard")
    if spec is None:
        return
    index, count = parse_shard(spec)
    kept = [item for item in items if is_kept(item.nodeid, index, count)]
    dropped = [item for item in items if not is_kept(item.nodeid, index, count)]
    if dropped:
        config.hook.pytest_deselected(items=dropped)
    items[:] = kept


@pytest.fixture
def anyio_backend() -> str:
    """Run `@pytest.mark.anyio` tests on asyncio only, the event loop used in production."""
    return "asyncio"
