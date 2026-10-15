"""Committed HTTP fixtures are scrubbed (AC-4.1.3, golden rule 5).

Every file under `tests/fixtures/` is scanned for things a recorded response must never carry
into the repository: email addresses, street addresses and token-shaped strings (JWTs, long hex
or base64 runs mixing cases and digits). Fixtures use obviously fake values instead.
"""

import re
from pathlib import Path

import pytest

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures"

_EMAIL = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)*\.[A-Za-z]{2,}")
_STREET_SUFFIXES = (
    "street|st|avenue|ave|road|rd|boulevard|blvd|lane|ln|drive|dr|court|ct|way|place|pl|"
    "terrace|highway|hwy|square|sq|parkway|pkwy"
)
_STREET = re.compile(
    rf"\b\d{{1,6}}[A-Za-z]?\s+(?:[A-Za-z][A-Za-z'.-]*\s+){{1,4}}(?:{_STREET_SUFFIXES})\b\.?",
    re.IGNORECASE,
)
# Hebrew street prefixes ("Rehov X 12", "רחוב הרצל 5", "רח' הרצל 5").
_STREET_HEBREW = re.compile(r"(?:\b(?:rehov|rechov)\b|רחוב|רח')\s*\S+\s+\d{1,5}", re.IGNORECASE)
_JWT = re.compile(r"eyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}")
_LONG_HEX = re.compile(r"(?<![A-Za-z0-9])[0-9a-fA-F]{32,}(?![A-Za-z0-9])")
_LONG_RUN = re.compile(r"[A-Za-z0-9+/_.=-]{32,}")


def _token_shaped(text: str) -> list[str]:
    found = _JWT.findall(text) + _LONG_HEX.findall(text)
    for run in _LONG_RUN.findall(text):
        # URL slugs and identifiers are long too, but a credential mixes cases and digits.
        mixed = (
            any(c.islower() for c in run)
            and any(c.isupper() for c in run)
            and any(c.isdigit() for c in run)
        )
        if mixed:
            found.append(run)
    return found


def find_unscrubbed(text: str) -> list[str]:
    """Return a description of every email, street address or token-shaped string in `text`."""
    problems = [f"email {m!r}" for m in _EMAIL.findall(text)]
    problems += [f"street address {m.group(0)!r}" for m in _STREET.finditer(text)]
    problems += [f"street address {m.group(0)!r}" for m in _STREET_HEBREW.finditer(text)]
    problems += [f"token-shaped string {m!r}" for m in _token_shaped(text)]
    return problems


def _fixture_files() -> list[Path]:
    return sorted(path for path in FIXTURES.rglob("*") if path.is_file())


def test_fixtures__exist() -> None:
    assert any(path.suffix == ".json" for path in (FIXTURES / "etsy").iterdir())


@pytest.mark.ac("AC-4.1.3")
@pytest.mark.parametrize("path", _fixture_files(), ids=lambda p: p.relative_to(FIXTURES).as_posix())
def test_committed_fixture__is_scrubbed(path: Path) -> None:
    problems = find_unscrubbed(path.read_text(encoding="utf-8"))

    assert problems == [], f"{path.relative_to(FIXTURES)} is not scrubbed: {problems}"


@pytest.mark.ac("AC-4.1.3")
@pytest.mark.parametrize(
    "leak",
    [
        '{"buyer_email": "dana.levi@gmail.com"}',
        '{"seller_email": "shop+orders@example.co.il"}',
        '{"first_line": "221B Baker Street"}',
        '{"formatted_address": "1600 Pennsylvania Ave NW"}',
        '{"first_line": "12 Rothschild Blvd."}',
        '{"first_line": "Rehov Herzl 12"}',
        '{"first_line": "רחוב הרצל 12"}',
        # Token samples are assembled at runtime so the secret scanner does not flag this file.
        '{"value": "'
        + "eyJ"
        + "hbGciOiJIUzI1NiJ9"
        + ".eyJ"
        + "zdWIiOiJmYWtlIn0"
        + ".c2ln"
        + 'bmF0dXJl"}',
        '{"value": "' + "9f86d081884c7d65" * 4 + '"}',
        '{"value": "12345678.' + "Qm9ndXNUb2tlbg" * 3 + '"}',
    ],
)
def test_find_unscrubbed__real_looking_values__are_reported(leak: str) -> None:
    assert find_unscrubbed(leak) != []


@pytest.mark.ac("AC-4.1.3")
@pytest.mark.parametrize(
    "clean",
    [
        '{"name": "Fake Buyer One", "first_line": null, "buyer_email": null}',
        '{"url": "https://www.etsy.com/listing/7000000001/fake-test-mug-for-htco-fixtures-only"}',
        '{"grandtotal": {"amount": 2599, "divisor": 100, "currency_code": "USD"}}',
        '{"status": "Paid", "created_timestamp": 1760000000, "quantity": 12}',
        '{"title": "Ten pieces, 12 colours, made to order"}',
    ],
)
def test_find_unscrubbed__fake_and_structural_values__pass(clean: str) -> None:
    assert find_unscrubbed(clean) == []
