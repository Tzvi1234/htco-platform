"""Notifications and email (slice 6.4): one notification and one email per recipient and key,
identifiers only, the Hebrew and English templates, delivery outcomes and security notices."""

import uuid
from datetime import UTC, datetime
from typing import override

import pytest

from htco_application.incidents import EVENTS, INCIDENT_OPENED
from htco_application.messaging import OutboxMessage, PendingDelivery
from htco_application.notifications import (
    EMAIL_DESTINATION,
    NOTIFICATION_REQUESTED,
    EmailDeliverer,
    EmailPermanentError,
    EmailTemporaryError,
    EventDeliverer,
    IncidentNotifications,
    NotificationRequest,
    NotificationService,
    Recipient,
    RequestedNotifications,
    SecurityNotices,
    device_family,
    notification_requested,
    render_email,
)
from htco_domain.notifications import (
    Locale,
    NotificationKind,
    Preference,
    UnsafeParameter,
    check_params,
    wants_email,
)
from tests.notification_fakes import (
    FakeEmail,
    Member,
    MemoryEmailDirectory,
    MemoryNotificationStore,
)

pytestmark = pytest.mark.anyio

ORG = uuid.UUID("00000000-0000-0000-0000-00000000e001")
SHOP = uuid.UUID("00000000-0000-0000-0000-00000000e002")
OWNER = uuid.UUID("00000000-0000-0000-0000-00000000e0a1")
ADMIN = uuid.UUID("00000000-0000-0000-0000-00000000e0a2")
OPS = uuid.UUID("00000000-0000-0000-0000-00000000e0a3")
INCIDENT = uuid.UUID("00000000-0000-0000-0000-00000000e0f1")
NOW = datetime(2026, 10, 25, 9, 0, tzinfo=UTC)
FAKE_BUYER = "Fakebuyer Zzname"
CHROME = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/141.0 Safari/537.36"


def members() -> MemoryNotificationStore:
    return MemoryNotificationStore(
        [
            Member(OWNER, "owner", "owner@example.test"),
            Member(ADMIN, "admin", "admin@example.test", Recipient(ADMIN, Locale.EN)),
            Member(OPS, "operations", "ops@example.test"),
        ]
    )


def opened_event(receipt_id: int = 4000000001) -> OutboxMessage:
    return OutboxMessage(
        organization_id=ORG,
        shop_id=SHOP,
        destination=EVENTS,
        idempotency_key=f"{INCIDENT_OPENED}:{INCIDENT}",
        payload={
            "event_type": INCIDENT_OPENED, "incident_id": str(INCIDENT),
            "cause": "tracking_missing", "severity": "medium", "receipt_id": receipt_id,
            "dedup_key": "k", "resource_kind": "order", "resource_id": "x",
        },
    )  # fmt: skip


def delivery(message: OutboxMessage) -> PendingDelivery:
    return PendingDelivery(id=uuid.uuid7(), message=message, attempts=0, delivered=False)


# --- What a notification may say (AC-6.4.2) -----------------------------------------------------


@pytest.mark.ac("AC-6.4.2")
@pytest.mark.parametrize(
    ("name", "value"),
    [("buyer_name", FAKE_BUYER), ("receipt_id", FAKE_BUYER), ("device", "<script>"),
     ("cause", "Tracking Missing!"), ("receipt_id", "0")],
)  # fmt: skip
def test_ac_6_4_2_only_identifiers_pass(name: str, value: str) -> None:
    with pytest.raises(UnsafeParameter):
        check_params({name: value})


def test_identifiers_pass_as_strings() -> None:
    assert check_params({"receipt_id": 4000000001, "severity": "high"}) == {
        "receipt_id": "4000000001",
        "severity": "high",
    }


def test_security_email_cannot_be_turned_off() -> None:
    off = Preference(in_app=False, email=False)
    assert wants_email(NotificationKind.SIGN_IN_NEW_DEVICE, off)
    assert not wants_email(NotificationKind.INCIDENT_OPENED, off)
    assert wants_email(NotificationKind.INCIDENT_OPENED, None)
    assert not wants_email(NotificationKind.INCIDENT_RESOLVED, None)


# --- One incident, one email (AC-6.4.1) ---------------------------------------------------------


@pytest.mark.ac("AC-6.4.1")
async def test_ac_6_4_1_one_incident_one_email_per_recipient_even_when_run_twice() -> None:
    store = members()
    service = NotificationService(store, clock=lambda: NOW)
    deliverer = EventDeliverer({INCIDENT_OPENED: IncidentNotifications(service)})

    for _ in range(3):
        await deliverer.deliver(delivery(opened_event()))

    assert {key[1] for key in store.rows} == {OWNER, ADMIN}  # owners and admins
    emails = [m for m in store.outbox.values() if m.destination == EMAIL_DESTINATION]
    assert len(emails) == 2
    assert {m.payload["locale"] for m in emails} == {"he", "en"}


@pytest.mark.ac("AC-6.4.1")
async def test_ac_6_4_1_the_email_job_twice_sends_one_email() -> None:
    store = members()
    await NotificationService(store, clock=lambda: NOW).notify(
        NotificationRequest(ORG, NotificationKind.INCIDENT_OPENED, "incident_opened:x",
                            {"receipt_id": 5, "cause": "tracking_missing", "severity": "low"})
    )  # fmt: skip
    email = FakeEmail()
    directory = MemoryEmailDirectory({OWNER: "owner@example.test", ADMIN: "admin@example.test"})
    deliverer = EmailDeliverer(
        email, directory, clock=lambda: NOW, reply_to="support@example.test",
        office_url="https://office.example.test",
    )  # fmt: skip
    message = next(m for m in store.outbox.values() if m.payload["user_id"] == str(OWNER))
    pending = delivery(message)

    await deliverer.deliver(pending)
    await deliverer.deliver(pending)  # a crash between the send and the mark

    assert len(email.sent) == 1  # the provider deduplicates by the outbox key
    [sent] = email.sent.values()
    assert sent.idempotency_key == message.idempotency_key
    assert sent.reply_to == "support@example.test"
    assert sent.sender.endswith("<noreply@office.ht-co.app>")


async def test_preferences_and_unrelated_events() -> None:
    store = members()
    store.members[0].recipient = Recipient(OWNER, preference=Preference(in_app=True, email=False))
    service = NotificationService(store, clock=lambda: NOW)
    deliverer = EventDeliverer({INCIDENT_OPENED: IncidentNotifications(service)})

    await deliverer.deliver(delivery(opened_event()))
    await deliverer.deliver(
        delivery(OutboxMessage(ORG, EVENTS, "other:1", {"event_type": "orders.ingested.v1"}))
    )

    assert len(store.rows) == 2
    assert [m.payload["user_id"] for m in store.outbox.values()] == [str(ADMIN)]


# --- Templates (AC-6.4.2) -----------------------------------------------------------------------


@pytest.mark.ac("AC-6.4.2")
@pytest.mark.parametrize("kind", list(NotificationKind))
@pytest.mark.parametrize("locale", list(Locale))
def test_ac_6_4_2_every_template_renders_with_identifiers_only(
    kind: NotificationKind, locale: Locale
) -> None:
    params = {"receipt_id": "4000000001", "cause": "tracking_missing", "severity": "high",
              "incident_id": str(INCIDENT), "device": "Chrome on Windows", "change": "enrolled",
              "rating": "2", "ship_by": "2026-10-30", "listing_id": "777"}  # fmt: skip
    rendered = render_email(kind, locale, params, link="https://office.example.test/x")
    assert rendered.subject
    assert "https://office.example.test/x" in rendered.text
    assert f'dir="{"rtl" if locale is Locale.HE else "ltr"}"' in rendered.html
    assert FAKE_BUYER not in rendered.text + rendered.html


def test_templates_refuse_buyer_data_from_a_tampered_outbox() -> None:
    with pytest.raises(UnsafeParameter):
        render_email(
            NotificationKind.INCIDENT_OPENED, Locale.EN, {"buyer": FAKE_BUYER}, link="https://x"
        )


def test_the_hebrew_incident_email_reads_in_hebrew() -> None:
    rendered = render_email(
        NotificationKind.INCIDENT_OPENED, Locale.HE,
        {"receipt_id": "4000000001", "cause": "tracking_missing", "severity": "medium"},
        link="https://office.example.test/incidents/1",
    )  # fmt: skip
    assert "הזמנה 4000000001" in rendered.subject
    assert "אין עדיין מספר מעקב" in rendered.text


# --- Delivery outcomes (AC-6.4.3) ---------------------------------------------------------------


def _email_message(user: uuid.UUID = OWNER) -> OutboxMessage:
    return OutboxMessage(
        organization_id=ORG,
        destination=EMAIL_DESTINATION,
        idempotency_key=f"email:k:{user}",
        payload={"notification_id": str(uuid.uuid4()), "user_id": str(user),
                 "kind": "incident_opened", "locale": "en",
                 "params": {"receipt_id": "7", "cause": "sync_drift", "severity": "low"}},
    )  # fmt: skip


def _deliverer(email: FakeEmail, directory: MemoryEmailDirectory) -> EmailDeliverer:
    return EmailDeliverer(
        email, directory, clock=lambda: NOW, reply_to="support@example.test",
        office_url="https://office.example.test/",
    )  # fmt: skip


@pytest.mark.ac("AC-6.4.3")
async def test_ac_6_4_3_a_temporary_failure_raises_so_the_outbox_retries_with_backoff() -> None:
    directory = MemoryEmailDirectory({OWNER: "owner@example.test"})
    deliverer = _deliverer(FakeEmail(fail=EmailTemporaryError("resend 503")), directory)

    with pytest.raises(EmailTemporaryError):
        await deliverer.deliver(delivery(_email_message()))

    assert directory.records == {}


@pytest.mark.ac("AC-6.4.3")
async def test_ac_6_4_3_a_permanent_failure_is_recorded_for_the_admin_and_not_retried() -> None:
    directory = MemoryEmailDirectory({OWNER: "owner@example.test"})
    pending = delivery(_email_message())

    await _deliverer(FakeEmail(fail=EmailPermanentError("resend 422")), directory).deliver(pending)

    assert directory.records[pending.id] == ("failed", None, "resend 422")


async def test_a_member_who_left_is_skipped_and_recorded() -> None:
    directory = MemoryEmailDirectory({})
    pending = delivery(_email_message())
    email = FakeEmail()

    await _deliverer(email, directory).deliver(pending)

    assert email.calls == 0
    assert directory.records[pending.id] == ("skipped", None, "no_member")


async def test_a_sent_email_links_to_the_incident() -> None:
    directory = MemoryEmailDirectory({OWNER: "owner@example.test"})
    email = FakeEmail()
    message = _email_message()
    payload = dict(message.payload)
    payload["params"] = {"incident_id": str(INCIDENT), "receipt_id": "7",
                         "cause": "sync_drift", "severity": "low"}  # fmt: skip
    pending = delivery(OutboxMessage(ORG, EMAIL_DESTINATION, "email:x", payload))

    await _deliverer(email, directory).deliver(pending)

    [sent] = email.sent.values()
    assert f"https://office.example.test/incidents/{INCIDENT}" in sent.text
    assert directory.records[pending.id][0] == "sent"
    assert "owner@example.test" not in repr(sent)


# --- Security notices (AC-6.4.4) ----------------------------------------------------------------


class NoticeStore:
    def __init__(self, agents: frozenset[str | None], orgs: tuple[uuid.UUID, ...] = (ORG,)) -> None:
        self.agents = agents
        self.orgs = orgs
        self.added: list[OutboxMessage] = []

    def organizations_of(self, user_id: uuid.UUID) -> tuple[uuid.UUID, ...]:  # noqa: ARG002
        return self.orgs

    def earlier_agents(self, user_id: uuid.UUID, *, before: uuid.UUID) -> frozenset[str | None]:  # noqa: ARG002
        return self.agents

    def add(self, message: OutboxMessage, *, now: datetime) -> uuid.UUID:  # noqa: ARG002
        self.added.append(message)
        return uuid.uuid4()


@pytest.mark.ac("AC-6.4.4")
async def test_ac_6_4_4_a_sign_in_from_a_new_device_sends_one_email_to_that_user() -> None:
    notice_store = NoticeStore(frozenset({"Firefox/1.0 (X11; Linux)"}))
    notices = SecurityNotices(notice_store, clock=lambda: NOW)
    session = uuid.uuid7()

    sent = await notices.signed_in(OWNER, session_id=session, user_agent=CHROME)

    assert sent == 1
    [event] = notice_store.added
    assert event.payload["kind"] == "sign_in_new_device"
    assert event.payload["user_id"] == str(OWNER)
    assert event.payload["params"] == {"device": "Chrome on Windows"}
    # The worker turns the request into one notification and one email for that user only.
    store = members()
    await RequestedNotifications(NotificationService(store, clock=lambda: NOW)).handle(event)
    await RequestedNotifications(NotificationService(store, clock=lambda: NOW)).handle(event)
    assert [key[1] for key in store.rows] == [OWNER]
    assert len(store.outbox) == 1


@pytest.mark.ac("AC-6.4.4")
async def test_ac_6_4_4_a_known_device_or_a_first_sign_in_sends_nothing() -> None:
    known = NoticeStore(frozenset({CHROME}))
    first = NoticeStore(frozenset())
    assert (
        await SecurityNotices(known, clock=lambda: NOW).signed_in(
            OWNER, session_id=uuid.uuid7(), user_agent=CHROME
        )
        == 0
    )
    assert (
        await SecurityNotices(first, clock=lambda: NOW).signed_in(
            OWNER, session_id=uuid.uuid7(), user_agent=CHROME
        )
        == 0
    )
    assert known.added == first.added == []


@pytest.mark.ac("AC-6.4.4")
async def test_ac_6_4_4_mfa_changes_and_shop_connections_notify_the_owners() -> None:
    notice_store = NoticeStore(frozenset())
    notices = SecurityNotices(notice_store, clock=lambda: NOW)

    await notices.mfa_changed(ADMIN, factor_id="factor-1", change="enrolled")
    await notices.shop(NotificationKind.SHOP_CONNECTED, ORG, SHOP, key="20261025T090000")
    await notices.shop(NotificationKind.SHOP_DISCONNECTED, ORG, SHOP, key="20261025T100000")

    store = members()
    handler = RequestedNotifications(NotificationService(store, clock=lambda: NOW))
    for event in notice_store.added:
        assert event.payload["event_type"] == NOTIFICATION_REQUESTED
        await handler.handle(event)
    assert {key[1] for key in store.rows} == {OWNER}  # owners only
    assert len(store.rows) == 3


async def test_a_failing_notice_never_blocks_the_action() -> None:
    class Broken(NoticeStore):
        @override
        def add(self, message: OutboxMessage, *, now: datetime) -> uuid.UUID:
            raise ConnectionError("down")

        @override
        def earlier_agents(self, user_id: uuid.UUID, *, before: uuid.UUID) -> frozenset[str | None]:
            raise ConnectionError("down")

    notices = SecurityNotices(Broken(frozenset({"x"})), clock=lambda: NOW)
    assert await notices.signed_in(OWNER, session_id=uuid.uuid7(), user_agent=CHROME) == 0
    assert await notices.shop(NotificationKind.SHOP_CONNECTED, ORG, SHOP, key="k") == 0


def test_device_families_and_requests() -> None:
    assert device_family(CHROME) == "Chrome on Windows"
    assert device_family(None) == "A browser on an unknown system"
    with pytest.raises(ValueError, match="names its user"):
        NotificationRequest(ORG, NotificationKind.SIGN_IN_NEW_DEVICE, "k")
    with pytest.raises(UnsafeParameter):
        notification_requested(
            NotificationRequest(ORG, NotificationKind.SHOP_CONNECTED, "k", {"name": FAKE_BUYER})
        )


async def test_email_only_preference_writes_no_visible_in_app_notification() -> None:
    """Codex review of #87: in_app=False hides the row from the bell; the email still goes."""
    store = members()
    store.members[0].recipient = Recipient(OWNER, preference=Preference(in_app=False, email=True))
    service = NotificationService(store, clock=lambda: NOW)

    await IncidentNotifications(service).handle(opened_event())

    owner_row = store.rows[(ORG, OWNER, f"incident_opened:{INCIDENT}")]
    assert owner_row.in_app is False
    assert any(m.payload["user_id"] == str(OWNER) for m in store.outbox.values())


async def test_a_resolved_incident_notifies_once() -> None:
    """Codex review of #87: `incidents.resolved.v1` has a subscriber."""
    store = members()
    service = NotificationService(store, clock=lambda: NOW)
    resolved = OutboxMessage(
        organization_id=ORG, shop_id=SHOP, destination=EVENTS,
        idempotency_key=f"incidents.resolved.v1:{INCIDENT}",
        payload={"event_type": "incidents.resolved.v1", "incident_id": str(INCIDENT),
                 "cause": "tracking_missing", "severity": "medium", "receipt_id": 4000000001},
    )  # fmt: skip
    deliverer = EventDeliverer({"incidents.resolved.v1": IncidentNotifications(service)})

    await deliverer.deliver(delivery(resolved))
    await deliverer.deliver(delivery(resolved))

    kinds = {(n.kind, n.dedup_key) for n in store.rows.values()}
    assert kinds == {(NotificationKind.INCIDENT_RESOLVED, f"incident_resolved:{INCIDENT}")}
    assert store.outbox == {}  # resolutions are in-app only by default
