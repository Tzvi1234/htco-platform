"""Small ports that many features share."""

import uuid
from collections.abc import Sequence
from typing import Protocol

from htco_application.kernel.types import TenantShop


class ShopOwners(Protocol):
    def organization_of_shop(self, shop_id: uuid.UUID) -> uuid.UUID | None: ...


class ConnectedShops(Protocol):
    """Connected shops by ID only, across organizations: what a per-shop fan-out needs."""

    def connected_shops(self) -> Sequence[TenantShop]: ...
