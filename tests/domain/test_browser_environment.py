"""A shop's browser environment (slice 5.16, ADR-0020, `execution-engine.md` §7): its seven
statuses, the transition table, the consent it is created with and its health reports."""

import uuid
from datetime import UTC, datetime, timedelta
from itertools import product

import pytest
from hypothesis import given
from hypothesis import strategies as st

from htco_domain.actors import ActorKind, ActorRef
from htco_domain.browser import (
    BrowserEnvironment,
    ConsentRecord,
    EnvironmentChange,
    EnvironmentStatus,
    HealthReport,
    check_environment_transition,
    environment_edges,
)
from htco_domain.state_machine import IllegalTransition

_E = EnvironmentStatus
NOW = datetime(2026, 10, 24, 12, 0, tzinfo=UTC)
OWNER = ActorRef(ActorKind.USER, str(uuid.UUID(int=1)))

# The table of execution-engine.md §7, written out again so the code cannot drift from the doc.
ALLOWED = {
    (_E.NOT_CONFIGURED, _E.PROVISIONING),
    (_E.PROVISIONING, _E.READY),
    (_E.PROVISIONING, _E.LOGIN_REQUIRED),
    (_E.PROVISIONING, _E.FAILED),
    (_E.READY, _E.LOGIN_REQUIRED),
    (_E.READY, _E.DEGRADED),
    (_E.READY, _E.SUSPENDED),
    (_E.DEGRADED, _E.SUSPENDED),
    (_E.LOGIN_REQUIRED, _E.SUSPENDED),
    (_E.LOGIN_REQUIRED, _E.READY),
    (_E.DEGRADED, _E.READY),
    (_E.DEGRADED, _E.FAILED),
    (_E.SUSPENDED, _E.READY),
    (_E.FAILED, _E.PROVISIONING),
    # Any configured environment can be deleted: back to NOT_CONFIGURED.
    *((status, _E.NOT_CONFIGURED) for status in _E if status is not _E.NOT_CONFIGURED),
}
ALL_PAIRS = list(product(_E, _E))


def consent() -> ConsentRecord:
    return ConsentRecord(
        text_version="browser-environment-2026-10", user_id=uuid.UUID(int=1), at=NOW,
        ip_hash="a" * 64,
    )  # fmt: skip


def environment(status: EnvironmentStatus = _E.PROVISIONING) -> BrowserEnvironment:
    return BrowserEnvironment(
        id=uuid.uuid7(),
        organization_id=uuid.uuid4(),
        shop_id=uuid.uuid4(),
        status=status,
        consent=consent(),
    )


def test_environment_status__has_the_seven_statuses() -> None:
    assert [s.name for s in _E] == [
        "NOT_CONFIGURED", "PROVISIONING", "READY", "LOGIN_REQUIRED", "DEGRADED", "SUSPENDED",
        "FAILED",
    ]  # fmt: skip


@pytest.mark.ac("AC-5.16.1")
@pytest.mark.parametrize(("current", "target"), sorted(ALLOWED))
def test_transition__allowed_edge__succeeds(
    current: EnvironmentStatus, target: EnvironmentStatus
) -> None:
    check_environment_transition(current, target)

    moved = environment(current).transition(target)

    assert moved.status is target


@pytest.mark.ac("AC-5.16.1")
@pytest.mark.parametrize(("current", "target"), [pair for pair in ALL_PAIRS if pair not in ALLOWED])
def test_transition__any_other_edge__is_refused(
    current: EnvironmentStatus, target: EnvironmentStatus
) -> None:
    with pytest.raises(IllegalTransition):
        check_environment_transition(current, target)
    with pytest.raises(IllegalTransition):
        environment(current).transition(target)


@pytest.mark.ac("AC-5.16.1")
@given(st.lists(st.sampled_from(list(_E)), max_size=20))
def test_transition__any_walk__stays_inside_the_table(targets: list[EnvironmentStatus]) -> None:
    current = environment(_E.PROVISIONING)

    for target in targets:
        if (current.status, target) in ALLOWED:
            current = current.transition(target)
        else:
            with pytest.raises(IllegalTransition):
                current.transition(target)

    assert current.status in _E


def test_only_ready__accepts_browser_actions() -> None:
    assert [s for s in _E if environment(s).accepts_browser_actions] == [_E.READY]


@pytest.mark.ac("AC-5.16.2")
def test_change__records_actor_reason_and_correlation() -> None:
    before = environment(_E.PROVISIONING)

    change = EnvironmentChange.of(
        before, _E.READY, by=OWNER, reason="preflight_passed", correlation_id="corr-1", at=NOW
    )

    assert (change.from_status, change.to_status) == (_E.PROVISIONING, _E.READY)
    assert (change.actor, change.reason, change.correlation_id) == (
        OWNER,
        "preflight_passed",
        "corr-1",
    )
    assert (change.environment_id, change.organization_id, change.shop_id) == (
        before.id,
        before.organization_id,
        before.shop_id,
    )


@pytest.mark.ac("AC-5.16.2")
def test_change__illegal_edge__is_refused() -> None:
    with pytest.raises(IllegalTransition):
        EnvironmentChange.of(
            environment(_E.SUSPENDED), _E.DEGRADED, by=OWNER, reason="x", correlation_id="c",
            at=NOW,
        )  # fmt: skip


@pytest.mark.ac("AC-5.16.2")
@pytest.mark.parametrize(("reason", "correlation"), [("", "c"), ("  ", "c"), ("r", ""), ("r", " ")])
def test_change__needs_a_reason_and_a_correlation(reason: str, correlation: str) -> None:
    with pytest.raises(ValueError, match="reason and a correlation"):
        EnvironmentChange.of(
            environment(), _E.READY, by=OWNER, reason=reason, correlation_id=correlation, at=NOW
        )


def test_change__long_reason__is_refused() -> None:
    with pytest.raises(ValueError, match="500"):
        EnvironmentChange.of(
            environment(), _E.READY, by=OWNER, reason="r" * 501, correlation_id="c", at=NOW
        )


def test_change__naive_time__is_refused() -> None:
    with pytest.raises(ValueError, match="UTC"):
        EnvironmentChange.of(
            environment(), _E.READY, by=OWNER, reason="r", correlation_id="c",
            at=datetime(2026, 10, 24),  # noqa: DTZ001 - the naive time under test
        )  # fmt: skip


def test_creation__is_the_edge_from_not_configured() -> None:
    change = EnvironmentChange.creation(
        environment(_E.PROVISIONING), by=OWNER, reason="created", correlation_id="c", at=NOW
    )

    assert (change.from_status, change.to_status) == (_E.NOT_CONFIGURED, _E.PROVISIONING)


def test_creation__of_another_status__is_refused() -> None:
    with pytest.raises(IllegalTransition):
        EnvironmentChange.creation(
            environment(_E.READY), by=OWNER, reason="created", correlation_id="c", at=NOW
        )


def test_environment__has_no_fingerprint_or_proxy_field() -> None:
    """ADR-0020: the honest design. A stock browser, one platform egress, nothing to disguise."""
    fields = {name.lower() for name in BrowserEnvironment.__dataclass_fields__}

    assert not {f for f in fields if any(w in f for w in ("proxy", "fingerprint", "user_agent"))}


@pytest.mark.parametrize(
    "changes",
    [
        {"text_version": " "},
        {"ip_hash": "not-a-hash"},
        {"at": datetime(2026, 10, 24)},  # noqa: DTZ001 - the naive time under test
    ],
)
def test_consent__incomplete__is_refused(changes: dict[str, object]) -> None:
    values: dict[str, object] = {
        "text_version": "v1",
        "user_id": uuid.UUID(int=1),
        "at": NOW,
        "ip_hash": "b" * 64,
    } | changes
    with pytest.raises(ValueError, match="consent"):
        ConsentRecord(**values)  # type: ignore[arg-type]


def test_health_report__problems_are_codes() -> None:
    report = HealthReport(ok=False, checked_at=NOW, problems=("egress_ip_mismatch",))

    assert report.problems == ("egress_ip_mismatch",)
    with pytest.raises(ValueError, match="code"):
        HealthReport(ok=False, checked_at=NOW, problems=("The IP was 203.0.113.9",))


def test_health_report__ok_has_no_problems() -> None:
    with pytest.raises(ValueError, match="problems"):
        HealthReport(ok=True, checked_at=NOW, problems=("disk_low",))


def test_health_report__naive_time__is_refused() -> None:
    with pytest.raises(ValueError, match="UTC"):
        HealthReport(ok=True, checked_at=NOW.replace(tzinfo=None) - timedelta(0))


def test_environment_edges__are_the_table() -> None:
    assert environment_edges() == ALLOWED
