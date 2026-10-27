"""Stable sharding of pytest items, used to split the API fuzzing across parallel CI jobs."""

import zlib
from collections.abc import Sequence


def parse_shard(spec: str) -> tuple[int, int]:
    """Parse `INDEX/COUNT` (for example `2/4`) into `(2, 4)`; raise `ValueError` if malformed."""
    index_text, separator, count_text = spec.partition("/")
    if not separator or not index_text.isdecimal() or not count_text.isdecimal():
        raise ValueError(f"shard must look like INDEX/COUNT, got {spec!r}")
    index, count = int(index_text), int(count_text)
    if count < 1 or index >= count:
        raise ValueError(f"shard index must be in [0, {count}), got {spec!r}")
    return index, count


def shard_of(nodeid: str, count: int) -> int:
    """The shard that owns a test; crc32 is stable across processes, unlike `hash()`."""
    return zlib.crc32(nodeid.encode()) % count


def select_shard(nodeids: Sequence[str], index: int, count: int) -> list[str]:
    """The node ids that belong to shard `index` of `count`, in their original order."""
    return [nodeid for nodeid in nodeids if shard_of(nodeid, count) == index]
