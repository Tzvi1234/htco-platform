"""Identity and access: who acts, and how strongly they proved it."""

from enum import StrEnum


class Role(StrEnum):
    """Organization roles (FR-IAM-02). Owner and Admin act on every shop of the organization."""

    OWNER = "owner"
    ADMIN = "admin"
    OPERATIONS = "operations"
    FINANCE = "finance"
    SUPPORT = "support"
    DEVELOPER = "developer"
    VIEWER = "viewer"


class AssuranceLevel(StrEnum):
    """Authenticator assurance of the session: `aal2` means a second factor was verified."""

    AAL1 = "aal1"
    AAL2 = "aal2"
