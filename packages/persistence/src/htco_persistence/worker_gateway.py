"""What the Action Gateway needs to know about a shop (slice 5.4): its Etsy shop ID and its probed
capabilities, read as `htco_worker` inside the job's organization in the worker; and, for the
API's owner writes (slice 6.8), its organization and the scopes of its Etsy grant.

Synchronous like the gateway's other ports (the action store, the guards).
"""

import uuid

from htco_persistence.database import Database


class PostgresShopFacts:
    def __init__(self, database: Database) -> None:
        self._database = database

    def etsy_shop_id(self, shop_id: uuid.UUID) -> int | None:
        """The connected Etsy shop behind the HTCO shop; None when it is not connected."""
        with self._database.connection() as connection:
            row = connection.execute(
                """
                select etsy_shop_id from app.shop_integrations
                where shop_id = %s and status = 'connected'
                """,
                [shop_id],
            ).fetchone()
        return None if row is None else int(row[0])

    def organization_of_shop(self, shop_id: uuid.UUID) -> uuid.UUID | None:
        """The shop's organization (the API's owner writes, slice 6.8); None when unknown."""
        with self._database.connection() as connection:
            row = connection.execute(
                "select organization_id from app.shops where id = %s", [shop_id]
            ).fetchone()
        return None if row is None else uuid.UUID(str(row[0]))

    def scopes_of(self, shop_id: uuid.UUID) -> frozenset[str]:
        """The OAuth scopes of the shop's connected Etsy grant (ADR-0022 §4); none when it is not
        connected, so every write that needs a scope is refused before any call."""
        with self._database.connection() as connection:
            row = connection.execute(
                """
                select scopes from app.shop_integrations
                where shop_id = %s and status = 'connected'
                """,
                [shop_id],
            ).fetchone()
        return frozenset() if row is None else frozenset(row[0])

    def available(self, shop_id: uuid.UUID, capability: str) -> bool:
        """False only when the last probe found the capability unavailable. A capability never
        probed yet is tried: Etsy's own answer (403) is then the refusal, never a write."""
        with self._database.connection() as connection:
            row = connection.execute(
                "select state from app.shop_capabilities where shop_id = %s and capability = %s",
                [shop_id, capability],
            ).fetchone()
        return row is None or row[0] != "unavailable"
