"""Secret values, references and grants (slice 5.17, ADR-0021)."""

import copy
import json
import logging
import pickle
import uuid
from dataclasses import asdict
from datetime import UTC, datetime, timedelta

import pytest
from hypothesis import given
from hypothesis import strategies as st

from htco_domain.secrets import (
    MASK,
    DisplayedCode,
    InjectionGrant,
    SecretKind,
    SecretRef,
    SecretValue,
    period_window,
)

NOW = datetime(2026, 10, 24, 10, 0, tzinfo=UTC)
ENV = uuid.uuid4()
_SECRETS = st.text(
    alphabet=st.characters(codec="utf-8", exclude_categories=["Cs"]), min_size=6, max_size=40
).filter(lambda s: s.strip() and s not in MASK and MASK not in s and "Secret" not in s)


@pytest.mark.ac("AC-5.17.1")
@given(_SECRETS)
def test_secret_value__never_shows_in_repr_str_format_or_errors(raw: str) -> None:
    secret = SecretValue(raw)

    shown = [repr(secret), str(secret), f"{secret}", f"{secret!r}", "%s" % secret,  # noqa: UP031
             repr([secret]), repr({"password": secret}), str(ValueError(secret))]  # fmt: skip

    assert all(raw not in text for text in shown)
    assert secret.reveal() == raw


@pytest.mark.ac("AC-5.17.1")
def test_secret_value__never_serializes() -> None:
    secret = SecretValue("hunter2-fake-password")

    with pytest.raises(TypeError):
        json.dumps({"password": secret})
    with pytest.raises(TypeError):
        pickle.dumps(secret)
    with pytest.raises(TypeError):
        copy.deepcopy(secret)
    with pytest.raises(TypeError):
        copy.copy(secret)
    with pytest.raises(TypeError):
        hash(secret)
    with pytest.raises(TypeError):
        asdict(DisplayedCode(secret, NOW, 30))


@pytest.mark.ac("AC-5.17.1")
def test_secret_value__not_in_a_log_record(caplog: pytest.LogCaptureFixture) -> None:
    secret = SecretValue("hunter2-fake-password")

    with caplog.at_level(logging.INFO):
        logging.getLogger("htco.test").info("signing in with %s", secret)
        logging.getLogger("htco.test").info("grant %r", {"value": secret})

    assert "hunter2" not in caplog.text
    assert MASK in caplog.text


def test_secret_value__compares_by_value_and_is_never_empty() -> None:
    assert SecretValue("abc123") == SecretValue("abc123")
    assert SecretValue("abc123") != SecretValue("abc124")
    assert SecretValue("abc123") != "abc123"
    with pytest.raises(ValueError, match="empty"):
        SecretValue("")


def test_secret_ref__totp_names_its_account() -> None:
    SecretRef(SecretKind.TOTP, ENV, uuid.uuid4())
    SecretRef(SecretKind.ETSY_PASSWORD, ENV)
    with pytest.raises(ValueError, match="account"):
        SecretRef(SecretKind.TOTP, ENV)
    with pytest.raises(ValueError, match="account"):
        SecretRef(SecretKind.ETSY_PASSWORD, ENV, uuid.uuid4())


def grant(**changes: object) -> InjectionGrant:
    values: dict[str, object] = {
        "grant_id": uuid.uuid4(),
        "execution_id": uuid.uuid4(),
        "environment_id": ENV,
        "secret_ref": SecretRef(SecretKind.ETSY_PASSWORD, ENV),
        "field": "password",
        "issued_at": NOW,
        "expires_at": NOW + timedelta(seconds=60),
    } | changes
    return InjectionGrant(**values)  # type: ignore[arg-type]


@pytest.mark.ac("AC-5.17.2")
def test_grant__lives_at_most_60_seconds() -> None:
    assert not grant().expired(NOW + timedelta(seconds=59))
    assert grant().expired(NOW + timedelta(seconds=60))
    with pytest.raises(ValueError, match="60"):
        grant(expires_at=NOW + timedelta(seconds=61))
    with pytest.raises(ValueError, match="60"):
        grant(expires_at=NOW)


@pytest.mark.ac("AC-5.17.2")
@pytest.mark.parametrize(
    "changes",
    [
        {"secret_ref": SecretRef(SecretKind.ETSY_PASSWORD, uuid.uuid4())},
        {"field": "Password field"},
        {"issued_at": datetime(2026, 10, 24)},  # noqa: DTZ001 - the naive time under test
    ],
)
def test_grant__bound_to_its_environment_and_one_field(changes: dict[str, object]) -> None:
    with pytest.raises(ValueError):  # noqa: PT011 - each case has its own message
        grant(**changes)


def test_grant__its_repr_holds_no_secret() -> None:
    assert "password" in repr(grant())  # the field name only; the grant holds no value


def test_period_window__rfc6238_counter_and_end() -> None:
    at = datetime.fromtimestamp(59, UTC)

    assert period_window(at, 30) == (1, datetime.fromtimestamp(60, UTC))
    with pytest.raises(ValueError, match="period"):
        period_window(at, 0)
