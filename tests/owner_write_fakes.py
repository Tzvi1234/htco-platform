"""The Owner's write actions (slices 6.6, 6.8) on the real Action Gateway, against fakes only.

The gateway is the real `ActionGatewayService` over the real action contracts; Etsy is the
in-memory `FakeEtsy` of the gateway tests, so nothing here reaches Etsy (AC-6.8.3).
"""

import uuid
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import timedelta

from htco_application.access.authentication import AuthContext, Membership
from htco_application.audit import AuditRecord
from htco_application.execution.channels import EtsyApiChannel
from htco_application.execution.gateway import Channel
from htco_application.execution.service import ActionGatewayService, GatewayParts, Principals
from htco_application.owner_writes import OWNER_WRITES, LastRun, OwnerWriteServices, WriteSwitches
from htco_domain.identity import AssuranceLevel, Role
from tests.application.test_etsy_owner_writes import (
    ETSY_SHOP,
    GRANTED,
    NOW,
    ORG,
    OWNER,
    RECEIPT,
    SHOP,
    WRITES,
    EtsyShops,
    FakeEtsy,
    office,
)
from tests.execution_fakes import Approvals, Capabilities, MemoryActionStore, Shops, real_catalog
from tests.idempotency_fakes import MemoryIdempotencyStore
from tests.order_intake_fakes import paid_receipt
from tests.relay_fakes import MemoryRelayStore

__all__ = ["ETSY_SHOP", "NOW", "ORG", "OWNER", "RECEIPT", "SHOP", "WRITES", "World", "caller"]


# Every owner-triggerable write's flag on (each test turns off what it refuses).
ALL_WRITES = frozenset(w.action for w in OWNER_WRITES)


@dataclass
class ShopFacts:
    scopes: frozenset[str] = GRANTED

    def organization_of_shop(self, shop_id: uuid.UUID) -> uuid.UUID | None:
        return ORG if shop_id == SHOP else None

    def scopes_of(self, shop_id: uuid.UUID) -> frozenset[str]:
        return self.scopes if shop_id == SHOP else frozenset()


@dataclass
class History:
    """`OwnerWriteHistory` over the gateway's in-memory store: the newest row per action."""

    store: MemoryActionStore

    def last_runs(
        self, organization_id: uuid.UUID, shop_id: uuid.UUID, actions: frozenset[str]
    ) -> Mapping[str, LastRun]:
        found: dict[str, LastRun] = {}
        for execution_id, key in self.store.ids.items():  # in insertion order: newest last
            row = self.store.rows[key]
            request = row.request
            if (
                request.organization_id != organization_id
                or request.shop_id != shop_id
                or request.action.name not in actions
            ):
                continue
            result = row.result
            found[request.action.name] = LastRun(
                execution_id=execution_id,
                state=row.state.value,
                outcome=None if result is None else result.outcome.value,
                error_code=None if result is None else result.error_code,
                verified=False if result is None else result.verified,
                output=None if result is None or result.output is None else dict(result.output),
                created_at=NOW,
                finished_at=NOW,
            )
        return found


def caller(
    role: Role = Role.OWNER,
    *,
    aal: AssuranceLevel = AssuranceLevel.AAL2,
    reauthenticated: timedelta | None = timedelta(minutes=1),
    user: uuid.UUID = OWNER,
) -> AuthContext:
    return AuthContext(
        user_id=user,
        assurance=aal,
        session_id="session-1",
        memberships=(Membership(ORG, role, frozenset({SHOP})),),
        reauthenticated_at=None if reauthenticated is None else NOW - reauthenticated,
    )


class World:
    """The services the API wires, with every part a fake but the gateway and its contracts."""

    def __init__(
        self,
        *,
        flags: frozenset[str] = ALL_WRITES,
        halted: bool = False,
        scopes: frozenset[str] = GRANTED,
    ) -> None:
        self.etsy = FakeEtsy(receipts={RECEIPT: paid_receipt(RECEIPT)})
        self.store = MemoryActionStore()
        self.relay = MemoryRelayStore()
        self.facts = ShopFacts(scopes)
        # The kill switch, read at every check: a test may replace it to pull it mid-press.
        self.halted: Callable[[], bool] = lambda: halted
        self.switches = WriteSwitches(flags, halted=self._halted)
        registry = office()
        catalog = real_catalog(registry)
        channel = EtsyApiChannel(self.etsy, EtsyShops(), writes=self.etsy, holidays=self.etsy)

        def gateway_for(principals: Principals) -> ActionGatewayService:
            return ActionGatewayService(
                GatewayParts(
                    catalog=catalog,
                    registry=lambda: registry,
                    store=self.store,
                    principals=principals,
                    shops=Shops({SHOP: ORG}),
                    approvals=Approvals(),
                    adapters={Channel.API: channel},
                    capabilities=Capabilities(),
                    clock=lambda: NOW,
                    scopes=self.facts,
                    write_flags=self.switches,
                )
            )

        self.services = OwnerWriteServices(
            catalog=catalog,
            gateway_for=gateway_for,
            shops=self.facts,
            history=History(self.store),
            switches=self.switches,
            relay=self.relay,
            idempotency=MemoryIdempotencyStore(),
        )

    def _halted(self) -> bool:
        return self.halted()

    def audits(self) -> list[AuditRecord]:
        return [s.audit for row in self.store.rows.values() for s in row.steps if s.audit]
