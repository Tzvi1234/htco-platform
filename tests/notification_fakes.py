"""Notification stores and an email port in memory, with the Postgres rules: one notification
per recipient and key, its email only with a new row, one delivery record per outbox message."""

import uuid
from collections.abc import Mapping
from dataclasses import dataclass, field, replace
from datetime import datetime

from htco_application.messaging import OutboxMessage
from htco_application.notifications import (
    EmailMessage,
    EmailPermanentError,
    EmailReceipt,
    EmailTemporaryError,
    Notification,
    Recipient,
)
from htco_domain.notifications import (
    Audience,
    NotificationKind,
    NotificationSeverity,
    Preference,
)


@dataclass
class Member:
    user_id: uuid.UUID
    role: str  # "owner", "admin", "operations", ...
    address: str
    recipient: Recipient | None = None
    # Slice 7.6: per-severity choices and muted shops, as the Postgres query joins them.
    severity_preferences: dict[NotificationSeverity, Preference] = field(default_factory=dict)
    muted_shops: set[uuid.UUID] = field(default_factory=set)


@dataclass
class MemoryNotificationStore:
    members: list[Member] = field(default_factory=list)
    rows: dict[tuple[uuid.UUID, uuid.UUID, str], Notification] = field(default_factory=dict)
    outbox: dict[str, OutboxMessage] = field(default_factory=dict)
    severities: list[NotificationSeverity] = field(default_factory=list)

    def recipients(  # noqa: PLR0913 - as the port
        self,
        organization_id: uuid.UUID,  # noqa: ARG002 - one organization
        audience: Audience,
        kind: NotificationKind,  # noqa: ARG002 - preferences are on the members
        *,
        user_id: uuid.UUID | None,
        shop_id: uuid.UUID | None,
        severity: NotificationSeverity,
    ) -> tuple[Recipient, ...]:
        self.severities.append(severity)
        roles = {
            Audience.THE_USER: None,
            Audience.OWNERS: {"owner"},
            Audience.OWNERS_AND_ADMINS: {"owner", "admin"},
        }[audience]
        return tuple(
            replace(
                m.recipient or Recipient(m.user_id),
                severity_preference=m.severity_preferences.get(severity),
                shop_muted=shop_id is not None and shop_id in m.muted_shops,
            )
            for m in self.members
            if (roles is None or m.role in roles)
            and (audience is not Audience.THE_USER or m.user_id == user_id)
        )

    def add_notifications(
        self,
        notifications: tuple[Notification, ...],
        emails: Mapping[uuid.UUID, OutboxMessage],
        *,
        now: datetime,  # noqa: ARG002 - the port
    ) -> int:
        created = 0
        for n in notifications:
            key = (n.organization_id, n.user_id, n.dedup_key)
            if key in self.rows:
                continue
            self.rows[key] = n
            created += 1
            email = emails.get(n.user_id)
            if email is not None:
                self.outbox.setdefault(email.idempotency_key, email)
        return created


@dataclass
class MemoryEmailDirectory:
    addresses: dict[uuid.UUID, str] = field(default_factory=dict)
    records: dict[uuid.UUID, tuple[str, str | None, str | None]] = field(default_factory=dict)

    def address_of(self, organization_id: uuid.UUID, user_id: uuid.UUID) -> str | None:  # noqa: ARG002 - one organization
        return self.addresses.get(user_id)

    def record_email(  # noqa: PLR0913 - the port
        self,
        *,
        outbox_id: uuid.UUID,
        organization_id: uuid.UUID,  # noqa: ARG002
        user_id: uuid.UUID,  # noqa: ARG002
        kind: NotificationKind,  # noqa: ARG002
        status: str,
        provider_id: str | None,
        error: str | None,
        now: datetime,  # noqa: ARG002
    ) -> None:
        self.records.setdefault(outbox_id, (status, provider_id, error))


@dataclass
class FakeEmail:
    """Deduplicates by idempotency key, like Resend; can fail on demand."""

    sent: dict[str, EmailMessage] = field(default_factory=dict)
    calls: int = 0
    fail: Exception | None = None

    async def send(self, message: EmailMessage) -> EmailReceipt:
        self.calls += 1
        if self.fail is not None:
            raise self.fail
        self.sent.setdefault(message.idempotency_key, message)
        return EmailReceipt(provider_id=f"fake-{len(self.sent)}")


__all__ = [
    "EmailPermanentError",
    "EmailTemporaryError",
    "FakeEmail",
    "Member",
    "MemoryEmailDirectory",
    "MemoryNotificationStore",
]
