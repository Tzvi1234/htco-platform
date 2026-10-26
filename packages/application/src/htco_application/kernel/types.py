"""Shared value types."""

import uuid
from dataclasses import dataclass

type JsonValue = str | int | float | bool | list[JsonValue] | dict[str, JsonValue] | None


@dataclass(frozen=True)
class TenantShop:
    """A shop and the organization that owns it, by ID only."""

    organization_id: uuid.UUID
    shop_id: uuid.UUID


@dataclass(frozen=True)
class Page[ItemT]:
    """One page of an offset-paginated list."""

    items: tuple[ItemT, ...]
    # How many items match the query in total, on every page.
    total_count: int
    offset: int

    @property
    def next_offset(self) -> int | None:
        following = self.offset + len(self.items)
        return following if self.items and following < self.total_count else None
