"""Identity and access repositories (slice 2.3)."""

from collections.abc import Sequence

import anyio

from htco_application.access.authentication import AuthenticatedUser, Membership
from htco_domain.identity import Role
from htco_persistence.database import Database

# Active memberships with their explicit shop grants. Run inside the user's own transaction, so
# RLS shows only the user's memberships and grants even without the `user_id` filter.
_MEMBERSHIPS = """
select m.organization_id,
       m.role,
       coalesce(array_agg(sa.shop_id) filter (where sa.shop_id is not null), '{}') as shop_ids
from app.memberships m
left join app.shop_access sa on sa.membership_id = m.id
where m.user_id = %s and m.revoked_at is null
group by m.id, m.organization_id, m.role
order by m.organization_id
"""


class PostgresMembershipReader:
    def __init__(self, database: Database) -> None:
        self._database = database

    async def memberships_of(self, user: AuthenticatedUser) -> Sequence[Membership]:
        return await anyio.to_thread.run_sync(self._read, user)

    def _read(self, user: AuthenticatedUser) -> list[Membership]:
        with self._database.user_transaction(user.claims) as connection:
            rows = connection.execute(_MEMBERSHIPS, [user.user_id]).fetchall()
        return [
            Membership(organization_id=row[0], role=Role(row[1]), granted_shops=frozenset(row[2]))
            for row in rows
        ]
