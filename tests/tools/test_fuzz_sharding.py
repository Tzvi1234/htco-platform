"""The fuzz shards split the API operations: every one runs exactly once (no fuzzing here)."""

from pathlib import Path

import pytest
import schemathesis
import yaml
from schemathesis.core.result import Ok

from tests.contract import test_api_fuzzing
from tests.sharding import FUZZ_FILE, is_kept, parse_shard, select_shard, shard_of
from tools.openapi import CONTRACT_PATH

SHARDS = 4
TEST_NAME = test_api_fuzzing.test_api__every_operation__conforms_to_the_contract.__name__


def _operation_ids() -> list[str]:
    schema = schemathesis.openapi.from_path(test_api_fuzzing.REPO_ROOT / CONTRACT_PATH)
    return [
        f"tests/contract/test_api_fuzzing.py::{TEST_NAME}[{operation.ok().label}]"
        for operation in schema.get_all_operations()
        if isinstance(operation, Ok)
    ]


def test_shards__partition_the_operations__each_runs_exactly_once() -> None:
    ids = _operation_ids()
    assert len(ids) > SHARDS  # a real contract, not an empty one
    parts = [select_shard(ids, index, SHARDS) for index in range(SHARDS)]
    flat = [nodeid for part in parts for nodeid in part]
    assert sorted(flat) == sorted(ids)  # union is everything
    assert len(set(flat)) == len(flat)  # and no shard overlaps another


def test_shards__are_stable_and_all_used() -> None:
    assert shard_of("a::b[GET /x]", 4) == shard_of("a::b[GET /x]", 4)
    assert shard_of("anything", 1) == 0
    ids = [f"t::{n}" for n in range(200)]
    assert all(select_shard(ids, index, SHARDS) for index in range(SHARDS))


@pytest.mark.parametrize("spec", ["", "3", "4/4", "-1/4", "a/4", "1/0", "1/"])
def test_parse_shard__malformed__raises(spec: str) -> None:
    with pytest.raises(ValueError, match="shard"):
        parse_shard(spec)


def test_parse_shard__well_formed__returns_index_and_count() -> None:
    assert parse_shard("2/4") == (2, 4)


def test_is_kept__other_tests__are_never_deselected() -> None:
    other = ["tests/api/test_auth.py::test_a", "tests/contract/test_openapi.py::test_b[x]"]
    for nodeid in other:
        assert all(is_kept(nodeid, index, SHARDS) for index in range(SHARDS))


def test_is_kept__fuzz_items__are_kept_by_exactly_one_shard() -> None:
    nodeid = f"{FUZZ_FILE}::{TEST_NAME}[GET /healthz]"
    assert sum(is_kept(nodeid, index, SHARDS) for index in range(SHARDS)) == 1


def test_ci_fuzz_matrix__shard_list_matches_the_total() -> None:
    root = Path(__file__).resolve().parents[2]
    workflow = yaml.safe_load((root / ".github/workflows/ci.yml").read_text(encoding="utf-8"))
    job = workflow["jobs"]["fuzz"]
    matrix = job["strategy"]["matrix"]
    assert matrix["shard"] == list(range(len(matrix["shard"])))
    assert matrix["total"] == [len(matrix["shard"])]
    command = next(step["run"] for step in job["steps"] if "just fuzz" in step.get("run", ""))
    assert "${{ matrix.shard }}/${{ matrix.total }}" in command
