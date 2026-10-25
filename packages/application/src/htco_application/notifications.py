"""Notifications and email (slice 6.4, FR-NTF-01, FR-EML-01).

**In-app.** A `NotificationRequest` names a kind, an organization (and shop), its parameters
(identifiers only, `check_params`) and a deduplication key. The service finds the recipients by
the kind's audience and their preferences and, in one transaction, writes one `notifications` row
per recipient (unique per recipient and key) and, for those who receive email, one outbox message
to `email.send` keyed `email:{key}:{user_id}`. So one incident produces one email even if a job
runs twice (AC-6.4.1): the second run finds the rows and writes nothing.

**Email.** The `email.send` deliverer renders the template in the recipient's language, looks up
the address at send time (it never travels in the outbox), and sends through the `EmailPort` with
the outbox key as the provider's idempotency key, `From: noreply@office.ht-co.app` and `Reply-To`
the support address. A temporary failure raises: the outbox retries with backoff and marks the
message dead after its last attempt. A permanent failure (the provider refuses the message) is
recorded as failed and not retried. Both are listed in the admin view (AC-6.4.3).

**Events.** `incidents.opened.v1` (slice 6.3) arrives through the outbox's `events.publish`
destination; `EventDeliverer` hands it to `IncidentNotifications`.
"""

import logging
import uuid
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field
from datetime import datetime
from html import escape
from typing import Any, Protocol

from htco_application.jobs import JsonValue
from htco_application.messaging import OutboxMessage, PendingDelivery
from htco_domain.notifications import (
    AUDIENCE,
    Audience,
    Locale,
    NotificationKind,
    Preference,
    check_params,
    wants_email,
    wants_in_app,
)

EMAIL_DESTINATION = "email.send"
EVENTS_DESTINATION = "events.publish"
INCIDENT_RESOLVED_EVENT = "incidents.resolved.v1"
INCIDENT_ESCALATED_EVENT = "incidents.escalated.v1"
SENDER = "HTCO Office <noreply@office.ht-co.app>"

_log = logging.getLogger(__name__)


@dataclass(frozen=True)
class NotificationRequest:
    organization_id: uuid.UUID
    kind: NotificationKind
    dedup_key: str  # one notification per recipient and key
    params: Mapping[str, object] = field(default_factory=dict)
    shop_id: uuid.UUID | None = None
    incident_id: uuid.UUID | None = None
    user_id: uuid.UUID | None = None  # for `Audience.THE_USER`
    correlation_id: str | None = None

    def __post_init__(self) -> None:
        if not 0 < len(self.dedup_key) <= 120:  # noqa: PLR2004 - column size
            raise ValueError("a notification key is 1 to 120 characters")
        if AUDIENCE[self.kind] is Audience.THE_USER and self.user_id is None:
            raise ValueError(f"{self.kind.value} names its user")


@dataclass(frozen=True)
class Recipient:
    user_id: uuid.UUID
    locale: Locale = Locale.HE
    preference: Preference | None = None


@dataclass(frozen=True)
class Notification:
    id: uuid.UUID
    organization_id: uuid.UUID
    user_id: uuid.UUID
    kind: NotificationKind
    params: Mapping[str, str]
    dedup_key: str
    created_at: datetime
    shop_id: uuid.UUID | None = None
    incident_id: uuid.UUID | None = None
    read_at: datetime | None = None
    # False: the recipient wants this kind by email only; the inbox does not show it.
    in_app: bool = True


class NotificationStore(Protocol):
    """Synchronous (psycopg's pool); run through `offload`."""

    def recipients(
        self, organization_id: uuid.UUID, audience: Audience, kind: NotificationKind, *,
        user_id: uuid.UUID | None, shop_id: uuid.UUID | None,
    ) -> tuple[Recipient, ...]: ...  # fmt: skip

    def add_notifications(
        self,
        notifications: tuple[Notification, ...],
        emails: Mapping[uuid.UUID, OutboxMessage],
        *,
        now: datetime,
    ) -> int:
        """Insert each notification unless its recipient has the key; the email of a recipient
        (by user) only with a new row. Returns how many rows were new."""
        ...


type Offload = Callable[[Callable[[], Any]], Awaitable[Any]]


async def _inline(work: Callable[[], Any]) -> Any:
    return work()


def email_message(
    notification: Notification, locale: Locale, correlation: str | None
) -> OutboxMessage:
    payload: dict[str, JsonValue] = {
        "notification_id": str(notification.id),
        "user_id": str(notification.user_id),
        "kind": notification.kind.value,
        "locale": locale.value,
        "params": dict(notification.params),
    }
    return OutboxMessage(
        organization_id=notification.organization_id,
        shop_id=notification.shop_id,
        destination=EMAIL_DESTINATION,
        idempotency_key=f"email:{notification.dedup_key}:{notification.user_id}"[:200],
        payload=payload,
        correlation_id=None if correlation is None else correlation[:200],
    )


@dataclass
class NotificationService:
    store: NotificationStore
    clock: Callable[[], datetime]
    new_id: Callable[[], uuid.UUID] = uuid.uuid7
    offload: Offload = _inline

    async def notify(self, request: NotificationRequest) -> int:
        """Writes the notifications and emails; returns how many notifications were new."""
        params = check_params(request.params)
        now = self.clock()
        recipients: tuple[Recipient, ...] = await self.offload(
            lambda: self.store.recipients(
                request.organization_id, AUDIENCE[request.kind], request.kind,
                user_id=request.user_id, shop_id=request.shop_id,
            )
        )  # fmt: skip
        notifications: list[Notification] = []
        emails: dict[uuid.UUID, OutboxMessage] = {}
        for recipient in recipients:
            if not wants_in_app(request.kind, recipient.preference) and not wants_email(
                request.kind, recipient.preference
            ):
                continue
            notification = Notification(
                id=self.new_id(),
                organization_id=request.organization_id,
                user_id=recipient.user_id,
                kind=request.kind,
                params=params,
                dedup_key=request.dedup_key,
                created_at=now,
                shop_id=request.shop_id,
                incident_id=request.incident_id,
                in_app=wants_in_app(request.kind, recipient.preference),
            )
            notifications.append(notification)
            if wants_email(request.kind, recipient.preference):
                emails[recipient.user_id] = email_message(
                    notification, recipient.locale, request.correlation_id
                )
        if not notifications:
            return 0
        frozen = tuple(notifications)
        created: int = await self.offload(
            lambda: self.store.add_notifications(frozen, emails, now=now)
        )
        return created


# --- Events into notifications ------------------------------------------------------------------


class EventSubscriber(Protocol):
    async def handle(self, message: OutboxMessage) -> None: ...


@dataclass
class IncidentNotifications:
    """`incidents.opened.v1`, `incidents.escalated.v1` and `incidents.resolved.v1` → one
    notification per owner and admin (an email too for an opening or an escalation, by
    default)."""

    service: NotificationService

    async def handle(self, message: OutboxMessage) -> None:
        payload = message.payload
        kind = {
            INCIDENT_RESOLVED_EVENT: NotificationKind.INCIDENT_RESOLVED,
            INCIDENT_ESCALATED_EVENT: NotificationKind.INCIDENT_ESCALATED,
        }.get(str(payload.get("event_type")), NotificationKind.INCIDENT_OPENED)
        incident_id = uuid.UUID(str(payload["incident_id"]))
        params: dict[str, object] = {
            "incident_id": str(incident_id),
            "cause": payload["cause"],
            "severity": payload["severity"],
        }
        if payload.get("receipt_id") is not None:
            params["receipt_id"] = payload["receipt_id"]
        await self.service.notify(
            NotificationRequest(
                organization_id=message.organization_id,
                kind=kind,
                dedup_key=f"{kind.value}:{incident_id}",
                params=params,
                shop_id=message.shop_id,
                incident_id=incident_id,
                correlation_id=message.correlation_id,
            )
        )


@dataclass
class EventDeliverer:
    """The `events.publish` destination: events with a subscriber are handed to it (which must
    be idempotent: a replayed delivery calls it again); all others are accepted as records."""

    subscribers: Mapping[str, EventSubscriber]

    async def deliver(self, delivery: PendingDelivery) -> None:
        event_type = delivery.message.payload.get("event_type")
        subscriber = self.subscribers.get(event_type) if isinstance(event_type, str) else None
        if subscriber is not None:
            await subscriber.handle(delivery.message)
        _log.info("event.accepted: %s outbox=%s", event_type, delivery.id)


# --- Email ---------------------------------------------------------------------------------------


@dataclass(frozen=True)
class EmailMessage:
    to: str = field(repr=False)
    subject: str
    text: str
    html: str
    idempotency_key: str
    reply_to: str
    sender: str = SENDER
    tags: Mapping[str, str] = field(default_factory=dict)


@dataclass(frozen=True)
class EmailReceipt:
    provider_id: str


class EmailTemporaryError(Exception):
    """The provider is unavailable or rate-limits: retry with backoff."""


class EmailPermanentError(Exception):
    """The provider refuses this message (invalid address, rejected domain): never retried.
    The message is a status code or a type name, never the address."""


class EmailPort(Protocol):
    async def send(self, message: EmailMessage) -> EmailReceipt: ...


class EmailDirectory(Protocol):
    """Synchronous; run through `offload`."""

    def address_of(self, organization_id: uuid.UUID, user_id: uuid.UUID) -> str | None:
        """The member's sign-in address; None when the user left the organization."""
        ...

    def record_email(  # noqa: PLR0913 - one delivery's facts
        self,
        *,
        outbox_id: uuid.UUID,
        organization_id: uuid.UUID,
        user_id: uuid.UUID,
        kind: NotificationKind,
        status: str,  # "sent", "failed", "skipped"
        provider_id: str | None,
        error: str | None,
        now: datetime,
    ) -> None: ...


@dataclass(frozen=True)
class RenderedEmail:
    subject: str
    text: str
    html: str


_RTL = {Locale.HE: "rtl", Locale.EN: "ltr"}
_TEMPLATES: Mapping[tuple[NotificationKind, Locale], tuple[str, str]] = {
    (NotificationKind.INCIDENT_OPENED, Locale.EN): (
        "Order {receipt_id}: needs attention",
        "An incident was opened for order {receipt_id} ({cause}, severity {severity}).",
    ),
    (NotificationKind.INCIDENT_OPENED, Locale.HE): (
        "הזמנה {receipt_id}: דורשת טיפול",
        "נפתח אירוע להזמנה {receipt_id} ({cause}, חומרה {severity}).",
    ),
    (NotificationKind.INCIDENT_RESOLVED, Locale.EN): (
        "Order {receipt_id}: resolved",
        "The incident for order {receipt_id} was resolved on evidence.",
    ),
    (NotificationKind.INCIDENT_RESOLVED, Locale.HE): (
        "הזמנה {receipt_id}: נפתר",
        "האירוע של הזמנה {receipt_id} נפתר על סמך ראיה.",
    ),
    (NotificationKind.INCIDENT_ESCALATED, Locale.EN): (
        "Order {receipt_id}: escalated",
        "The incident for order {receipt_id} ({cause}, severity {severity}) has waited past its"
        " limit and needs you now.",
    ),
    (NotificationKind.INCIDENT_ESCALATED, Locale.HE): (
        "הזמנה {receipt_id}: הוסלם",
        "האירוע של הזמנה {receipt_id} ({cause}, חומרה {severity}) חיכה מעבר למותר"
        " ודורש אתכם עכשיו.",
    ),
    (NotificationKind.ALERT_SHIP_BY, Locale.EN): (
        "Order {receipt_id} ships by {ship_by}",
        "Order {receipt_id} has no shipment yet and must ship by {ship_by}.",
    ),
    (NotificationKind.ALERT_SHIP_BY, Locale.HE): (
        "הזמנה {receipt_id} צריכה להישלח עד {ship_by}",
        "להזמנה {receipt_id} עדיין אין משלוח, והיא צריכה להישלח עד {ship_by}.",
    ),
    (NotificationKind.ALERT_LOW_REVIEW, Locale.EN): (
        "A {rating}-star review",
        "A new review of {rating} stars arrived for order {receipt_id}.",
    ),
    (NotificationKind.ALERT_LOW_REVIEW, Locale.HE): (
        "ביקורת של {rating} כוכבים",
        "התקבלה ביקורת חדשה של {rating} כוכבים להזמנה {receipt_id}.",
    ),
    (NotificationKind.ALERT_OUT_OF_STOCK, Locale.EN): (
        "Listing {listing_id} is out of stock",
        "Listing {listing_id} has no stock left.",
    ),
    (NotificationKind.ALERT_OUT_OF_STOCK, Locale.HE): (
        "מוצר {listing_id} אזל מהמלאי",
        "למוצר {listing_id} לא נשאר מלאי.",
    ),
    (NotificationKind.RELAY_TRACKING_NEEDED, Locale.EN): (
        "Order {receipt_id}: copy the tracking number",
        "Order {receipt_id} has no tracking in Etsy yet. Copy it from AutoVA into the office.",
    ),
    (NotificationKind.RELAY_TRACKING_NEEDED, Locale.HE): (
        "הזמנה {receipt_id}: להעתיק מספר מעקב",
        "להזמנה {receipt_id} עדיין אין מספר מעקב ב-Etsy. העתיקו אותו מ-AutoVA למשרד.",
    ),
    (NotificationKind.SIGN_IN_NEW_DEVICE, Locale.EN): (
        "New sign-in to HTCO Office",
        "Your account signed in from a new device ({device}). If this was not you, change your"
        " password now.",
    ),
    (NotificationKind.SIGN_IN_NEW_DEVICE, Locale.HE): (
        "כניסה חדשה ל-HTCO Office",
        "נרשמה כניסה לחשבון ממכשיר חדש ({device}). אם הכניסה לא הייתה שלך, שנו את הסיסמה עכשיו.",
    ),
    (NotificationKind.MFA_CHANGED, Locale.EN): (
        "Two-step verification changed",
        "A member's two-step verification was {change}.",
    ),
    (NotificationKind.MFA_CHANGED, Locale.HE): (
        "האימות הדו-שלבי השתנה",
        "האימות הדו-שלבי של חבר צוות {change}.",
    ),
    (NotificationKind.SHOP_CONNECTED, Locale.EN): (
        "A shop was connected",
        "A shop was connected to HTCO.",
    ),
    (NotificationKind.SHOP_CONNECTED, Locale.HE): ("חנות חוברה", "חנות חוברה ל-HTCO."),
    (NotificationKind.SHOP_DISCONNECTED, Locale.EN): (
        "A shop was disconnected",
        "A shop was disconnected from HTCO.",
    ),
    (NotificationKind.SHOP_DISCONNECTED, Locale.HE): ("חנות נותקה", "חנות נותקה מ-HTCO."),
    (NotificationKind.INTEGRATION_REAUTH, Locale.EN): (
        "Reconnect your Etsy shop",
        "Etsy needs you to sign in again so HTCO can keep reading your shop.",
    ),
    (NotificationKind.INTEGRATION_REAUTH, Locale.HE): (
        "חברו מחדש את חנות ה-Etsy",
        "Etsy מבקשת להתחבר מחדש כדי ש-HTCO תמשיך לקרוא את החנות.",
    ),
    (NotificationKind.REFRESH_TOKEN_EXPIRING, Locale.EN): (
        "Your Etsy connection expires soon",
        "Reconnect the shop within a few days to keep HTCO reading it.",
    ),
    (NotificationKind.REFRESH_TOKEN_EXPIRING, Locale.HE): (
        "החיבור ל-Etsy יפוג בקרוב",
        "חברו את החנות מחדש בימים הקרובים כדי ש-HTCO תמשיך לקרוא אותה.",
    ),
}
_WORDS: Mapping[Locale, Mapping[str, str]] = {
    Locale.EN: {
        "tracking_missing": "no tracking yet",
        "sync_drift": "Etsy and HTCO differ",
        "tracking_mismatch": "tracking mismatch",
        "automation_dead_lettered": "an automation stopped",
        "low": "low", "medium": "medium", "high": "high", "critical": "critical",
        "enrolled": "turned on", "removed": "removed",
        "open": "Open in HTCO Office", "footer": "HTCO Office. Replies reach our support team.",
    },
    Locale.HE: {
        "tracking_missing": "אין עדיין מספר מעקב",
        "sync_drift": "Etsy ו-HTCO לא תואמים",  # noqa: RUF001 - Hebrew
        "tracking_mismatch": "מספר מעקב לא תואם",
        "automation_dead_lettered": "אוטומציה נעצרה",
        "low": "נמוכה", "medium": "בינונית", "high": "גבוהה", "critical": "קריטית",
        "enrolled": "הופעל", "removed": "הוסר",
        "open": "לפתוח ב-HTCO Office", "footer": "HTCO Office. תשובה למייל מגיעה לצוות התמיכה.",
    },
}  # fmt: skip


def render_email(
    kind: NotificationKind, locale: Locale, params: Mapping[str, str], *, link: str
) -> RenderedEmail:
    """The subject, plain text and HTML in the reader's language. Parameters are checked again
    here (they arrive from the outbox) and escaped in the HTML."""
    checked = check_params(params)
    words = _WORDS[locale]
    values = {name: words.get(value, value) for name, value in checked.items()}
    subject_template, body_template = _TEMPLATES[(kind, locale)]
    subject = subject_template.format_map(_Missing(values))
    body = body_template.format_map(_Missing(values))
    text = f"{body}\n\n{words['open']}: {link}\n\n{words['footer']}\n"
    direction = _RTL[locale]
    html = (
        f'<!doctype html><html lang="{locale.value}" dir="{direction}"><body '
        'style="font-family:Arial,sans-serif;color:#1f1f1f;background:#ffffff">'
        f"<p>{escape(body)}</p>"
        f'<p><a href="{escape(link, quote=True)}" style="color:#d9480f">'
        f"{escape(words['open'])}</a></p>"
        f'<p style="color:#6b6b6b;font-size:12px">{escape(words["footer"])}</p></body></html>'
    )
    return RenderedEmail(subject=subject, text=text, html=html)


class _Missing(dict[str, str]):
    def __missing__(self, key: str) -> str:
        return "-"


@dataclass
class EmailDeliverer:
    """The `email.send` destination."""

    email: EmailPort
    directory: EmailDirectory
    clock: Callable[[], datetime]
    reply_to: str
    office_url: str
    offload: Offload = _inline

    async def deliver(self, delivery: PendingDelivery) -> None:
        message = delivery.message
        payload = message.payload
        user_id = uuid.UUID(str(payload["user_id"]))
        kind = NotificationKind(str(payload["kind"]))
        locale = Locale(str(payload.get("locale", Locale.HE.value)))
        raw = payload.get("params")
        params = {str(k): str(v) for k, v in raw.items()} if isinstance(raw, dict) else {}
        organization_id = message.organization_id
        address = await self.offload(lambda: self.directory.address_of(organization_id, user_id))
        if address is None:
            await self._record(
                delivery,
                user_id=user_id,
                kind=kind,
                status="skipped",
                provider_id=None,
                error="no_member",
            )
            return
        rendered = render_email(kind, locale, params, link=self._link(kind, params))
        try:
            receipt = await self.email.send(
                EmailMessage(
                    to=address,
                    subject=rendered.subject,
                    text=rendered.text,
                    html=rendered.html,
                    idempotency_key=message.idempotency_key,
                    reply_to=self.reply_to,
                    tags={"kind": kind.value},
                )
            )
        except EmailPermanentError as error:
            await self._record(
                delivery,
                user_id=user_id,
                kind=kind,
                status="failed",
                provider_id=None,
                error=str(error)[:64] or "refused",
            )
            return
        await self._record(
            delivery,
            user_id=user_id,
            kind=kind,
            status="sent",
            provider_id=receipt.provider_id,
            error=None,
        )

    def _link(self, kind: NotificationKind, params: Mapping[str, str]) -> str:
        base = self.office_url.rstrip("/")
        incident = params.get("incident_id")
        if (
            kind
            in {
                NotificationKind.INCIDENT_OPENED,
                NotificationKind.INCIDENT_ESCALATED,
                NotificationKind.INCIDENT_RESOLVED,
            }
            and incident
        ):
            return f"{base}/incidents/{incident}"
        if kind.is_security:
            return f"{base}/settings/security"
        return f"{base}/notifications"

    async def _record(  # noqa: PLR0913 - one delivery's facts
        self,
        delivery: PendingDelivery,
        *,
        user_id: uuid.UUID,
        kind: NotificationKind,
        status: str,
        provider_id: str | None,
        error: str | None,
    ) -> None:
        now = self.clock()
        await self.offload(
            lambda: self.directory.record_email(
                outbox_id=delivery.id, organization_id=delivery.message.organization_id,
                user_id=user_id, kind=kind, status=status, provider_id=provider_id, error=error,
                now=now,
            )
        )  # fmt: skip


# --- Requests from other processes (`notifications.requested.v1`) --------------------------------

NOTIFICATION_REQUESTED = "notifications.requested.v1"


def notification_requested(request: NotificationRequest) -> OutboxMessage:
    """The request as an event, for a process that does not write notifications itself (the
    API): the worker's `RequestedNotifications` turns it into notifications and emails."""
    params: dict[str, JsonValue] = dict(check_params(request.params))
    payload: dict[str, JsonValue] = {
        "event_type": NOTIFICATION_REQUESTED,
        "kind": request.kind.value,
        "dedup_key": request.dedup_key,
        "params": params,
        "user_id": None if request.user_id is None else str(request.user_id),
        "incident_id": None if request.incident_id is None else str(request.incident_id),
    }
    key = f"{NOTIFICATION_REQUESTED}:{request.organization_id}:{request.dedup_key}"
    return OutboxMessage(
        organization_id=request.organization_id,
        shop_id=request.shop_id,
        destination=EVENTS_DESTINATION,
        idempotency_key=key[:200],
        payload=payload,
        correlation_id=None if request.correlation_id is None else request.correlation_id[:200],
    )


@dataclass
class RequestedNotifications:
    service: NotificationService

    async def handle(self, message: OutboxMessage) -> None:
        payload = message.payload
        raw = payload.get("params")
        user = payload.get("user_id")
        incident = payload.get("incident_id")
        await self.service.notify(
            NotificationRequest(
                organization_id=message.organization_id,
                kind=NotificationKind(str(payload["kind"])),
                dedup_key=str(payload["dedup_key"]),
                params=dict(raw) if isinstance(raw, dict) else {},
                shop_id=message.shop_id,
                user_id=None if user is None else uuid.UUID(str(user)),
                incident_id=None if incident is None else uuid.UUID(str(incident)),
                correlation_id=message.correlation_id,
            )
        )


# --- Security notices (AC-6.4.4) -----------------------------------------------------------------

_BROWSERS = (
    ("Edg/", "Edge"),
    ("Firefox/", "Firefox"),
    ("Chrome/", "Chrome"),
    ("Safari/", "Safari"),
)
_SYSTEMS = (
    ("Android", "Android"), ("iPhone", "iOS"), ("iPad", "iOS"), ("Windows", "Windows"),
    ("Mac OS X", "macOS"), ("Linux", "Linux"),
)  # fmt: skip


def device_family(user_agent: str | None) -> str:
    """For example "Chrome on Windows": enough to recognise a device; no version, no IP."""
    agent = user_agent or ""
    browser = next((name for marker, name in _BROWSERS if marker in agent), "A browser")
    system = next((name for marker, name in _SYSTEMS if marker in agent), "an unknown system")
    return f"{browser} on {system}"


class SecurityNoticeStore(Protocol):
    """Synchronous (the API's pool); run through `offload`."""

    def organizations_of(self, user_id: uuid.UUID) -> tuple[uuid.UUID, ...]:
        """The organizations where the user is an active member."""
        ...

    def earlier_agents(self, user_id: uuid.UUID, *, before: uuid.UUID) -> frozenset[str | None]:
        """User agents of the user's sessions other than `before` (revoked ones included)."""
        ...

    def add(self, message: OutboxMessage, *, now: datetime) -> uuid.UUID: ...


@dataclass
class SecurityNotices:
    """Security emails requested from the API process (sign-in, MFA, shops) and the worker
    (re-authentication, refresh-token expiry). A failure to request one is logged, never
    raised: it must not block the action it reports."""

    store: SecurityNoticeStore
    clock: Callable[[], datetime]
    offload: Offload = _inline

    async def signed_in(
        self, user_id: uuid.UUID, *, session_id: uuid.UUID, user_agent: str | None
    ) -> int:
        """One email to the user per organization when the device was never seen before. The
        very first sign-in of an account is not "new": there is nothing to compare it with."""
        try:
            earlier: frozenset[str | None] = await self.offload(
                lambda: self.store.earlier_agents(user_id, before=session_id)
            )
        except Exception as error:
            _log.warning("notifications.notice_failed: sign_in %s", type(error).__name__)
            return 0
        if not earlier or user_agent in earlier:
            return 0
        device = device_family(user_agent)
        return await self._each_organization(
            user_id,
            lambda org: NotificationRequest(
                organization_id=org,
                kind=NotificationKind.SIGN_IN_NEW_DEVICE,
                dedup_key=f"sign_in:{session_id}",
                params={"device": device},
                user_id=user_id,
            ),
        )

    async def mfa_changed(self, user_id: uuid.UUID, *, factor_id: str, change: str) -> int:
        key = f"mfa:{user_id}:{factor_id[:40]}:{change}"[:120]
        return await self._each_organization(
            user_id,
            lambda org: NotificationRequest(
                organization_id=org,
                kind=NotificationKind.MFA_CHANGED,
                dedup_key=key,
                params={"change": change},
            ),
        )

    async def shop(
        self, kind: NotificationKind, organization_id: uuid.UUID, shop_id: uuid.UUID, *, key: str
    ) -> int:
        """Shop connected or disconnected, re-authentication needed, refresh token expiring."""
        return await self._add(
            NotificationRequest(
                organization_id=organization_id,
                kind=kind,
                dedup_key=f"{kind.value}:{shop_id}:{key}"[:120],
                params={"shop_id": str(shop_id)},
                shop_id=shop_id,
            )
        )

    async def _each_organization(
        self, user_id: uuid.UUID, build: Callable[[uuid.UUID], NotificationRequest]
    ) -> int:
        try:
            organizations: tuple[uuid.UUID, ...] = await self.offload(
                lambda: self.store.organizations_of(user_id)
            )
        except Exception as error:
            _log.warning("notifications.notice_failed: %s", type(error).__name__)
            return 0
        sent = 0
        for organization in organizations:
            sent += await self._add(build(organization))
        return sent

    async def _add(self, request: NotificationRequest) -> int:
        message = notification_requested(request)
        now = self.clock()
        try:
            await self.offload(lambda: self.store.add(message, now=now))
        except Exception as error:
            _log.warning(
                "notifications.notice_failed: %s %s", request.kind.value, type(error).__name__
            )
            return 0
        return 1
