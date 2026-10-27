"""Shared pytest configuration for the HTCO platform test suite."""

import pytest

from tests.sharding import parse_shard, shard_of


def pytest_addoption(parser: pytest.Parser) -> None:
    parser.addoption(
        "--fuzz-shard",
        default=None,
        metavar="INDEX/COUNT",
        help="Run only the tests of this shard (a stable crc32 of the node id), e.g. 0/4.",
    )


def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:
    spec = config.getoption("--fuzz-shard")
    if spec is None:
        return
    index, count = parse_shard(spec)
    kept = [item for item in items if shard_of(item.nodeid, count) == index]
    dropped = [item for item in items if shard_of(item.nodeid, count) != index]
    if dropped:
        config.hook.pytest_deselected(items=dropped)
    items[:] = kept


@pytest.fixture
def anyio_backend() -> str:
    """Run `@pytest.mark.anyio` tests on asyncio only, the event loop used in production."""
    return "asyncio"
