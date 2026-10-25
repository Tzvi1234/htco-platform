"""The sealed browser profiles on Postgres (slice 8A.1): `app.browser_profile_archives` and the
profile columns of `app.shop_browser_environments`, on the browser worker's role.

Use it on a `Database(role="htco_browser")` inside `tenant_scope(organization_id)`: the role's
policies show one organization's rows only, so an environment or archive of another
organization reads as missing and cannot be written (AC-8A.1.2). The archive column holds sealed
bytes only; this adapter never opens them.
"""

import uuid
from typing import Any

from htco_application.browser_profiles import SealedArchive
from htco_domain.browser import BrowserEnvironment, EnvironmentStatus
from htco_persistence.database import Database


def _environment(row: tuple[Any, ...]) -> BrowserEnvironment:
    return BrowserEnvironment(
        id=row[0], organization_id=row[1], shop_id=row[2], status=EnvironmentStatus(row[3]),
        challenge_count=row[4], last_health_at=row[5], last_health_ok=row[6],
    )  # fmt: skip


class PostgresBrowserProfiles:
    """`ProfileArchiveStore` and `ProfileRecords` for the browser worker."""

    def __init__(self, database: Database) -> None:
        self._database = database

    def by_id(self, environment_id: uuid.UUID) -> BrowserEnvironment | None:
        with self._database.connection() as connection:
            row = connection.execute(
                """
                select id, organization_id, shop_id, status, challenge_count, last_health_at,
                       last_health_ok
                from app.shop_browser_environments where id = %s
                """,
                [environment_id],
            ).fetchone()
        return None if row is None else _environment(row)

    def record_profile(
        self, environment_id: uuid.UUID, *, storage_key: str | None, key_id: str | None
    ) -> None:
        with self._database.connection() as connection, connection.transaction():
            connection.execute(
                """
                update app.shop_browser_environments
                set profile_storage_key = %s, profile_key_id = %s
                where id = %s
                """,
                [storage_key, key_id, environment_id],
            )

    def get(self, environment_id: uuid.UUID) -> SealedArchive | None:
        with self._database.connection() as connection:
            row = connection.execute(
                """
                select environment_id, organization_id, shop_id, key_id, archive, sealed_at
                from app.browser_profile_archives where environment_id = %s
                """,
                [environment_id],
            ).fetchone()
        if row is None:
            return None
        return SealedArchive(
            environment_id=row[0], organization_id=row[1], shop_id=row[2], key_id=row[3],
            blob=bytes(row[4]), sealed_at=row[5],
        )  # fmt: skip

    def put(self, archive: SealedArchive) -> None:
        with self._database.connection() as connection, connection.transaction():
            connection.execute(
                """
                insert into app.browser_profile_archives
                  (environment_id, organization_id, shop_id, key_id, archive, sealed_at)
                values (%s, %s, %s, %s, %s, %s)
                on conflict (environment_id) do update
                  set key_id = excluded.key_id, archive = excluded.archive,
                      sealed_at = excluded.sealed_at
                """,
                [
                    archive.environment_id,
                    archive.organization_id,
                    archive.shop_id,
                    archive.key_id,
                    archive.blob,
                    archive.sealed_at,
                ],
            )

    def delete(self, environment_id: uuid.UUID) -> None:
        with self._database.connection() as connection, connection.transaction():
            connection.execute(
                "delete from app.browser_profile_archives where environment_id = %s",
                [environment_id],
            )
