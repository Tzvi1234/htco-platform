"""Browser configuration profiles on Postgres (slice 8A.2b): `app.browser_configuration_profiles`.

The API's login role writes a version and its audit row in one transaction. The insert names the
version it expects to follow (`where coalesce(max(version), 0) = expected`), and the unique key
on `(environment_id, version)` lets only one of two concurrent writers in; the database's
trigger refuses any other number either way (SQLSTATE 40001, a lost race). Every method hops
to a thread (AUD-05).
"""

import uuid
from typing import Any

import anyio
import psycopg

from htco_application.audit import AuditRecord
from htco_application.browser_configuration import ConfiguredEnvironment
from htco_domain.actors import ActorKind, ActorRef
from htco_domain.browser import EnvironmentStatus
from htco_domain.browser_configuration import ConfigurationVersion, ShopBrowserSettings
from htco_persistence.audit import append_audit
from htco_persistence.database import Database

_COLUMNS = (
    "environment_id, organization_id, shop_id, version, locale, shop_languages, egress_id, "
    "actor_kind, actor_id, correlation_id, created_at"
)

_APPEND = """
insert into app.browser_configuration_profiles
  (id, organization_id, shop_id, environment_id, version, locale, shop_languages, egress_id,
   actor_kind, actor_id, correlation_id, created_at)
select %(id)s, %(organization_id)s, %(shop_id)s, %(environment_id)s, %(version)s, %(locale)s,
       %(shop_languages)s, %(egress_id)s, %(actor_kind)s, %(actor_id)s, %(correlation_id)s,
       %(created_at)s
where coalesce(
  (select max(p.version) from app.browser_configuration_profiles p
   where p.environment_id = %(environment_id)s), 0
) = %(previous)s
on conflict (environment_id, version) do nothing
returning id
"""


def _version(row: tuple[Any, ...]) -> ConfigurationVersion:
    return ConfigurationVersion(
        environment_id=row[0], organization_id=row[1], shop_id=row[2], version=row[3],
        settings=ShopBrowserSettings(locale=row[4], egress_id=row[6]),
        shop_languages=tuple(row[5]), created_by=ActorRef(ActorKind(row[7]), row[8]),
        correlation_id=row[9], created_at=row[10],
    )  # fmt: skip


class PostgresBrowserConfigurations:
    """The configuration store, on the API's login role."""

    def __init__(self, database: Database) -> None:
        self._database = database

    async def organization_of_shop(self, shop_id: uuid.UUID) -> uuid.UUID | None:
        def run() -> uuid.UUID | None:
            with self._database.connection() as connection:
                row = connection.execute(
                    "select organization_id from app.shops where id = %s", [shop_id]
                ).fetchone()
            return None if row is None else row[0]

        return await anyio.to_thread.run_sync(run)

    async def environment_of_shop(self, shop_id: uuid.UUID) -> ConfiguredEnvironment | None:
        def run() -> ConfiguredEnvironment | None:
            with self._database.connection() as connection:
                row = connection.execute(
                    "select id, organization_id, shop_id, status "
                    "from app.shop_browser_environments where shop_id = %s",
                    [shop_id],
                ).fetchone()
            if row is None:
                return None
            return ConfiguredEnvironment(row[0], row[1], row[2], EnvironmentStatus(row[3]))

        return await anyio.to_thread.run_sync(run)

    async def current(self, environment_id: uuid.UUID) -> ConfigurationVersion | None:
        def run() -> ConfigurationVersion | None:
            with self._database.connection() as connection:
                row = connection.execute(
                    f"select {_COLUMNS} from app.browser_configuration_profiles "  # noqa: S608 - constant names
                    "where environment_id = %s order by version desc limit 1",
                    [environment_id],
                ).fetchone()
            return None if row is None else _version(row)

        return await anyio.to_thread.run_sync(run)

    async def append(self, version: ConfigurationVersion, audit: AuditRecord) -> bool:
        def run() -> bool:
            try:
                return write()
            except psycopg.errors.SerializationFailure:
                # The trigger's refusal: another writer committed this version meanwhile.
                return False

        def write() -> bool:
            with self._database.connection() as connection, connection.transaction():
                created = connection.execute(
                    _APPEND,
                    {
                        "id": uuid.uuid7(),
                        "organization_id": version.organization_id,
                        "shop_id": version.shop_id,
                        "environment_id": version.environment_id,
                        "version": version.version,
                        "previous": version.version - 1,
                        "locale": version.settings.locale,
                        "shop_languages": list(version.shop_languages),
                        "egress_id": version.settings.egress_id,
                        "actor_kind": version.created_by.kind.value,
                        "actor_id": version.created_by.id,
                        "correlation_id": version.correlation_id,
                        "created_at": version.created_at,
                    },
                ).fetchone()
                if created is None:
                    return False
                append_audit(connection, audit)
            return True

        return await anyio.to_thread.run_sync(run)
