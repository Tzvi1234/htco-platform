"""`htco admin rotate-token-keys` (slice 4.2, AC-4.2.2): re-wrap stored tokens to the current key.

The API keeps serving while it runs: the key ring still holds the previous keys, so rows not yet
re-wrapped stay readable. The command prints the result as one JSON line (with the rows still on
each key ID) and never a key.
"""

import base64
import io
import json
import os

import pytest

from htco_api.cli import EXIT_CONFIGURATION_ERROR, main
from htco_platform.crypto import KeyRing

KEY_A = base64.b64encode(b"A" * 32).decode()
KEY_B = base64.b64encode(b"B" * 32).decode()
ARGS = ["admin", "rotate-token-keys"]


class FakeRotator:
    def __init__(self, pending: int) -> None:
        self.pending = pending
        self.started = self.stopped = False
        self.batch_sizes: list[int] = []

    async def start(self) -> None:
        self.started = True

    async def stop(self) -> None:
        self.stopped = True

    async def rotate_keys(self, *, batch_size: int) -> int:
        self.batch_sizes.append(batch_size)
        rotated, self.pending = self.pending, 0
        return rotated

    async def key_usage(self) -> dict[str, int]:
        return {"key-b": 3}


@pytest.fixture
def environment(monkeypatch: pytest.MonkeyPatch) -> pytest.MonkeyPatch:
    for name in list(os.environ):
        if name.startswith("HTCO_"):
            monkeypatch.delenv(name)
    monkeypatch.setenv("HTCO_DATABASE_URL", "postgresql://htco@db.invalid/htco")
    monkeypatch.setenv("HTCO_TOKEN_VAULT_KEY", f"key-b:{KEY_B}")
    monkeypatch.setenv("HTCO_TOKEN_VAULT_PREVIOUS_KEYS", f"key-a:{KEY_A}")
    return monkeypatch


@pytest.mark.ac("AC-4.2.2")
@pytest.mark.usefixtures("environment")
def test_rotate__rewraps_with_the_current_key_and_prints_json() -> None:
    rotator = FakeRotator(pending=3)
    rings: list[KeyRing] = []
    out = io.StringIO()

    def new_rotator(url: str, ring: KeyRing) -> FakeRotator:
        assert url == "postgresql://htco@db.invalid/htco"
        rings.append(ring)
        return rotator

    code = main([*ARGS, "--batch-size", "50"], new_rotator=new_rotator, stdout=out)

    assert code == 0
    assert json.loads(out.getvalue()) == {
        "rotated": 3,
        "current_key_id": "key-b",
        "rows_by_key_id": {"key-b": 3},  # when only the current ID is left, drop the old keys
    }
    assert rings[0].key_ids == ("key-b", "key-a")
    assert rotator.batch_sizes == [50]
    assert rotator.started
    assert rotator.stopped
    assert KEY_A not in out.getvalue()
    assert KEY_B not in out.getvalue()


@pytest.mark.usefixtures("environment")
def test_rotate__default_batch_size() -> None:
    rotator = FakeRotator(pending=0)

    assert main(ARGS, new_rotator=lambda _url, _ring: rotator, stdout=io.StringIO()) == 0
    assert rotator.batch_sizes == [100]


def test_rotate__without_a_vault_key__configuration_error(
    environment: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    environment.delenv("HTCO_TOKEN_VAULT_KEY")
    environment.delenv("HTCO_TOKEN_VAULT_PREVIOUS_KEYS")

    code = main(ARGS, new_rotator=lambda _url, _ring: FakeRotator(0))

    assert code == EXIT_CONFIGURATION_ERROR
    assert "HTCO_TOKEN_VAULT_KEY" in capsys.readouterr().err


@pytest.mark.ac("AC-4.2.3")
def test_rotate__invalid_key__configuration_error_without_the_value(
    environment: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    short = base64.b64encode(b"too-short-key").decode()
    environment.setenv("HTCO_TOKEN_VAULT_KEY", f"key-b:{short}")

    code = main(ARGS, new_rotator=lambda _url, _ring: FakeRotator(0))

    assert code == EXIT_CONFIGURATION_ERROR
    captured = capsys.readouterr()
    assert "HTCO_TOKEN_VAULT_KEY" in captured.err
    assert short not in captured.err + captured.out


@pytest.mark.usefixtures("environment")
@pytest.mark.parametrize("size", ["0", "-1", "10001", "many"])
def test_rotate__batch_size_out_of_range__usage_error(size: str) -> None:
    with pytest.raises(SystemExit) as caught:
        main([*ARGS, "--batch-size", size], new_rotator=lambda _url, _ring: FakeRotator(0))
    assert caught.value.code == 2
