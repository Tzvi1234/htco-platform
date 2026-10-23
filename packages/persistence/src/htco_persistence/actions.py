"""The Action Gateway's store on Postgres (slice 5.12): `app.action_executions`.

`claim` inserts the row with `on conflict do nothing` on `(organization_id, action_name,
idempotency_key)`, so concurrent submissions have one winner. `record` writes a step, its audit
record and its `actions.executed.v1` outbox message in one transaction.
"""

import uuid
from datetime import datetime
from typing import Any

from psycopg.types.json import Jsonb

from htco_application.execution.gateway import (
    ActionOutcome,
    ActionRequest,
    ActionResult,
    Channel,
)
from htco_application.execution.service import Claim, Finish
from htco_domain.actions import ACTION_FINAL_STATES, ActionState
from htco_persistence.audit import append_audit
from htco_persistence.database import Database
from htco_persistence.messaging import add_outbox_in

_COLUMNS = "id, state, outcome, channel, attempts, verified, error_code, output, approval_id"


def _stored_result(row: tuple[Any, ...]) -> ActionResult | None:
    if row[2] is None:
        return None
    return ActionResult(
        execution_id=row[0],
        outcome=ActionOutcome(row[2]),
        attempts=row[4],
        channel=None if row[3] is None else Channel(row[3]),
        output=row[7],
        verified=row[5],
        error_code=row[6],
    )


class PostgresActionStore:
    def __init__(self, database: Database) -> None:
        self._database = database

    def claim(self, request: ActionRequest, *, request_hash: str, now: datetime) -> Claim:
        with self._database.connection() as connection, connection.transaction():
            created = connection.execute(
                """
                insert into app.action_executions
                  (id, organization_id, shop_id, action_name, action_version, idempotency_key,
                   request_hash, state, actor_kind, actor_id, on_behalf_of_user, correlation_id,
                   causation_id, approval_id, created_at)
                values (%s, %s, %s, %s, %s, %s, %s, 'received', %s, %s, %s, %s, %s, %s, %s)
                on conflict (organization_id, action_name, idempotency_key) do nothing
                returning id
                """,
                [
                    uuid.uuid7(),
                    request.organization_id,
                    request.shop_id,
                    request.action.name,
                    request.action.version,
                    request.idempotency_key,
                    request_hash,
                    request.actor.kind.value,
                    request.actor.id,
                    request.on_behalf_of_user,
                    request.correlation_id,
                    request.causation_id,
                    request.approval_id,
                    now,
                ],
            ).fetchone()
            if created is not None:
                return Claim(created[0], created=True, state=ActionState.RECEIVED, result=None)
            row = connection.execute(
                f"""
                select {_COLUMNS} from app.action_executions
                where organization_id = %s and action_name = %s and idempotency_key = %s
                """,  # noqa: S608 - constant column list
                [request.organization_id, request.action.name, request.idempotency_key],
            ).fetchone()
        if row is None:  # pragma: no cover - the conflict means it exists
            raise RuntimeError("the action execution disappeared")
        state = ActionState(row[1])
        return Claim(
            execution_id=row[0],
            created=False,
            state=state,
            result=_stored_result(row) if state in ACTION_FINAL_STATES else None,
            approval_id=row[8],
        )

    def record(self, execution_id: uuid.UUID, step: Finish, *, now: datetime) -> None:
        result = step.result
        final = step.state in ACTION_FINAL_STATES
        with self._database.connection() as connection, connection.transaction():
            connection.execute(
                """
                update app.action_executions
                set state = %s, outcome = %s, channel = %s, attempts = attempts + %s,
                    verified = %s, error_code = %s, reason = %s, output = %s,
                    evidence = %s, approval_id = coalesce(%s, approval_id),
                    finished_at = case when %s then %s else finished_at end
                where id = %s
                """,
                [
                    step.state.value,
                    result.outcome.value,
                    None if result.channel is None else result.channel.value,
                    result.attempts,
                    result.verified,
                    result.error_code,
                    step.reason,
                    Jsonb(dict(result.output))
                    if step.store_output and result.output is not None
                    else None,
                    Jsonb([]),
                    step.approval_id,
                    final,
                    now,
                    execution_id,
                ],
            )
            if step.audit is not None:
                append_audit(connection, step.audit)
            if step.event is not None:
                add_outbox_in(connection, step.event, now=now)
