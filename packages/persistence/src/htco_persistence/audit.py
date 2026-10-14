"""The audit writer (slice 2.5): appends to `audit.audit_log` inside the caller's transaction."""

from typing import Any

import psycopg
from psycopg.types.json import Jsonb

from htco_application.audit import AuditRecord

_INSERT = """
insert into audit.audit_log
  (id, organization_id, actor_kind, actor_id, action, entity_type, entity_id, before, after,
   correlation_id)
values (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
"""


def append_audit(connection: psycopg.Connection[Any], record: AuditRecord) -> None:
    connection.execute(
        _INSERT,
        [
            record.id,
            record.organization_id,
            record.actor.kind.value,
            record.actor.id,
            record.action,
            record.entity_type,
            record.entity_id,
            None if record.before is None else Jsonb(dict(record.before)),
            None if record.after is None else Jsonb(dict(record.after)),
            record.correlation_id,
        ],
    )
