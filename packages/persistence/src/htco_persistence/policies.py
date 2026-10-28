"""Policies on Postgres (slice 7.2): `app.policies` and the append-only `app.policy_decisions`.

Publishing is serialized per organization by a transaction-scoped advisory lock, so two
concurrent changes get two consecutive versions instead of one failing on the unique key; the
version and its audit record commit together. The API publishes with its login role; the worker
reads policies and records decisions in its job's organization (RLS).
"""

import uuid
from collections.abc import Callable, Sequence
from datetime import datetime
from typing import Any

from psycopg.types.json import Jsonb

from htco_application.audit import AuditRecord
from htco_application.policies import RecordedDecision
from htco_domain.actors import ActorKind, ActorRef
from htco_domain.policy import PolicyDocument, PolicyVersion
from htco_persistence.audit import append_audit
from htco_persistence.database import Database

_COLUMNS = "organization_id, version, document, created_by_kind, created_by_id, created_at"
_LATEST = f"""
select {_COLUMNS} from app.policies
where organization_id = %s order by version desc limit 1
"""  # noqa: S608 - a constant column list
_MAX_CAPABILITY = 200


def _version(row: Sequence[Any]) -> PolicyVersion:
    return PolicyVersion(
        organization_id=row[0],
        version=row[1],
        document=PolicyDocument.from_json(row[2]),
        created_by=ActorRef(ActorKind(row[3]), row[4]),
        created_at=row[5],
    )


class PostgresPolicyStore:
    def __init__(self, database: Database) -> None:
        self._database = database

    def latest(self, organization_id: uuid.UUID) -> PolicyVersion | None:
        with self._database.connection() as connection:
            row = connection.execute(_LATEST, [organization_id]).fetchone()
        return None if row is None else _version(row)

    def publish(  # noqa: PLR0913 - the version's columns and its audit record
        self,
        organization_id: uuid.UUID,
        document: PolicyDocument,
        *,
        created_by: ActorRef,
        now: datetime,
        audit: Callable[[int], AuditRecord],
        only_if_none: bool = False,
    ) -> PolicyVersion:
        with self._database.connection() as connection, connection.transaction():
            connection.execute(
                "select pg_advisory_xact_lock(hashtextextended(%s, 0))",
                [f"app.policies:{organization_id}"],
            )
            latest = connection.execute(_LATEST, [organization_id]).fetchone()
            if only_if_none and latest is not None:
                return _version(latest)
            version = 1 if latest is None else latest[1] + 1
            row = connection.execute(
                f"""
                insert into app.policies
                  (id, organization_id, version, document, created_by_kind, created_by_id,
                   created_at)
                values (%s, %s, %s, %s, %s, %s, %s)
                returning {_COLUMNS}
                """,  # noqa: S608 - a constant column list
                [
                    uuid.uuid7(), organization_id, version, Jsonb(document.to_json()),
                    created_by.kind.value, created_by.id, now,
                ],
            ).fetchone()  # fmt: skip
            append_audit(connection, audit(version))
        if row is None:  # pragma: no cover - an insert returns its row
            raise RuntimeError("the policy insert returned nothing")
        return _version(row)

    def record(self, decision: RecordedDecision) -> None:
        d = decision.decision
        with self._database.connection() as connection:
            connection.execute(
                """
                insert into app.policy_decisions
                  (id, organization_id, policy_version, shop_id, actor_kind, actor_id, capability,
                   verdict, reason, correlation_id, decided_at)
                values (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                """,
                [
                    decision.id, decision.organization_id, d.policy_version, decision.shop_id,
                    d.actor.kind.value, d.actor.id, d.capability[:_MAX_CAPABILITY],
                    d.verdict.value, d.reason.value, decision.correlation_id,
                    decision.decided_at,
                ],
            )  # fmt: skip
