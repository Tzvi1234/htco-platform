"""Who acted: every record names its actor with one of these kinds (ADR-0012)."""

from dataclasses import dataclass
from enum import StrEnum
from typing import override


class ActorKind(StrEnum):
    USER = "user"  # a person signed in to HTCO
    EMPLOYEE = "employee"  # an AI employee of the workforce
    AUTOMATION = "automation"  # a deterministic workflow of the Automation Department
    ADVISOR = "advisor"  # a shared decision service such as Jev
    SYSTEM = "system"  # platform machinery: schedulers, monitors, the API itself


@dataclass(frozen=True)
class ActorRef:
    kind: ActorKind
    id: str

    def __post_init__(self) -> None:
        if not self.id.strip():
            raise ValueError("actor id must not be blank")

    @override
    def __str__(self) -> str:
        return f"{self.kind.value}:{self.id}"
