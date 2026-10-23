"""Secrets and one-time codes (ADR-0021, `execution-engine.md` §6, slice 5.17).

`SecretValue` holds a password, a TOTP secret or a one-time code. It never shows its value: its
`repr`, `str` and format are a fixed mask, it cannot be pickled, copied into JSON or hashed, and
comparison is constant-time. Only `reveal()` returns the value, and only the code that types it
into a page (the browser worker, Phase 8A) or seals it in the vault calls it.

`SecretRef` names a secret without holding it, and `InjectionGrant` is a single-use permission to
receive one secret for one field of one execution in one environment, valid for at most 60 s.
`DisplayedCode` is the dashboard's current TOTP code with its countdown.
"""

import hmac
import re
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import StrEnum
from typing import Never, override

MASK = "<secret>"
MAX_GRANT_TTL = timedelta(seconds=60)
_FIELD = re.compile(r"^[a-z][a-z0-9_]{0,63}$")


class SecretValue:
    """A value that never prints, logs, serializes or pickles."""

    __slots__ = ("__value",)

    def __init__(self, value: str) -> None:
        if not value:
            raise ValueError("a secret is not empty")
        self.__value = value

    def reveal(self) -> str:
        return self.__value

    @override
    def __repr__(self) -> str:
        return f"SecretValue({MASK})"

    @override
    def __str__(self) -> str:
        return MASK

    @override
    def __format__(self, format_spec: str) -> str:
        return MASK

    @override
    def __eq__(self, other: object) -> bool:
        if not isinstance(other, SecretValue):
            return NotImplemented
        return hmac.compare_digest(self.__value.encode(), other.reveal().encode())

    __hash__ = None  # type: ignore[assignment]

    @override
    def __reduce__(self) -> Never:
        raise TypeError("a secret cannot be pickled")

    def __copy__(self) -> Never:
        raise TypeError("a secret cannot be copied")

    def __deepcopy__(self, memo: object) -> Never:
        raise TypeError("a secret cannot be copied")


class SecretKind(StrEnum):
    ETSY_PASSWORD = "etsy_password"  # noqa: S105 - a kind, not a password
    TOTP = "totp"
    PARTNER_PASSWORD = "partner_password"  # noqa: S105 - a kind, not a password


@dataclass(frozen=True)
class SecretRef:
    kind: SecretKind
    environment_id: uuid.UUID
    account_id: uuid.UUID | None = None  # the TOTP account, for kind TOTP

    def __post_init__(self) -> None:
        if (self.kind is SecretKind.TOTP) != (self.account_id is not None):
            raise ValueError("a TOTP secret names its account, and only a TOTP secret does")


@dataclass(frozen=True)
class InjectionGrant:
    """Opaque to callers; the broker keeps it until it is redeemed once."""

    grant_id: uuid.UUID
    execution_id: uuid.UUID
    environment_id: uuid.UUID
    secret_ref: SecretRef
    field: str  # the form field it is typed into, for example "password"
    issued_at: datetime
    expires_at: datetime

    def __post_init__(self) -> None:
        if self.secret_ref.environment_id != self.environment_id:
            raise ValueError("a grant is for a secret of its own environment")
        if not _FIELD.match(self.field):
            raise ValueError("a grant names one form field")
        if self.issued_at.tzinfo is None or self.expires_at.tzinfo is None:
            raise ValueError("a grant's times are in UTC")
        if not timedelta(0) < self.expires_at - self.issued_at <= MAX_GRANT_TTL:
            raise ValueError("a grant lives for at most 60 seconds")

    def expired(self, now: datetime) -> bool:
        return now >= self.expires_at


@dataclass(frozen=True)
class DisplayedCode:
    """The dashboard's code view: never logged, never cached."""

    code: SecretValue
    period_ends_at: datetime
    period_seconds: int


def period_window(at: datetime, period_seconds: int) -> tuple[int, datetime]:
    """The TOTP counter of `at` and when its period ends (RFC 6238)."""
    if period_seconds < 1:
        raise ValueError("a TOTP period is at least one second")
    counter = int(at.timestamp()) // period_seconds
    ends = datetime.fromtimestamp((counter + 1) * period_seconds, tz=at.tzinfo)
    return counter, ends
