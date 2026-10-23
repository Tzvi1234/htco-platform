"""The Action Gateway's guards on Postgres (slice 5.15): state every worker shares.

- `PostgresRateLimiter`: counters per key in aligned windows (each UTC second and day) in
  `jobs.rate_windows`. One upsert per window adds the cost only while it stays inside the budget,
  in one transaction, so concurrent workers can never spend more than the budget together.
- `PostgresBreakers`: one row per key in `jobs.circuit_breakers`, changed under a row lock with
  the rules of `htco_domain.guards`. A closed breaker is read without a lock.
- `PostgresSlots`: leased slots in `jobs.concurrency_slots`; an expired lease (its worker died)
  is taken over, and a late release by the old holder frees nothing.
"""

import uuid
from collections.abc import Callable
from datetime import datetime, timedelta
from typing import Any

from psycopg import Connection

from htco_application.execution.guards import RateBudget
from htco_domain.guards import (
    Breaker,
    BreakerPolicy,
    BreakerState,
    admit,
    day_window,
    record,
    second_window,
)
from htco_persistence.database import Database

# Old windows are deleted when a new one starts.
_KEEP = {"second": timedelta(minutes=1), "day": timedelta(days=2)}

_SPEND = """
insert into jobs.rate_windows as w (key, scope, starts_at, used) values (%s, %s, %s, %s)
on conflict (key, scope, starts_at) do update set used = w.used + excluded.used
  where w.used + excluded.used <= %s
returning (xmax = 0) as inserted
"""


class _Spent(Exception):  # noqa: N818 - the budget is spent: roll the transaction back
    def __init__(self, retry_at: datetime) -> None:
        super().__init__("rate budget spent")
        self.retry_at = retry_at


class PostgresRateLimiter:
    def __init__(self, database: Database) -> None:
        self._database = database

    def acquire(self, budget: RateBudget, *, now: datetime, cost: int = 1) -> datetime | None:
        if not 1 <= cost <= budget.per_second:
            raise ValueError("a call costs between 1 and the per-second budget")
        day, next_day = day_window(now)
        second, next_second = second_window(now)
        windows = (
            ("day", day, budget.per_day, next_day),
            ("second", second, budget.per_second, next_second),
        )
        try:
            with self._database.connection() as connection, connection.transaction():
                for scope, start, limit, refill in windows:
                    _spend(connection, budget.key, scope, start, cost, limit, refill)
        except _Spent as spent:
            return spent.retry_at
        return None


def _spend(  # noqa: PLR0913, PLR0917 - one window
    connection: Connection[Any],
    key: str,
    scope: str,
    start: datetime,
    cost: int,
    limit: int,
    refill: datetime,
) -> None:
    """Add `cost` to the window, or raise `_Spent` (rolling the transaction back)."""
    row = connection.execute(_SPEND, [key, scope, start, cost, limit]).fetchone()
    if row is None:
        raise _Spent(refill)
    if row[0]:  # a new window: forget the old ones
        connection.execute(
            "delete from jobs.rate_windows where key = %s and scope = %s and starts_at < %s",
            [key, scope, start - _KEEP[scope]],
        )


_BREAKER_COLUMNS = "state, failures, window_started_at, opened_at, probe_at"


_Step = Callable[[Breaker], tuple[Breaker, datetime | None]]


def _breaker(row: tuple[Any, ...] | None) -> Breaker:
    if row is None:
        return Breaker()
    state, failures, window_started_at, opened_at, probe_at = row
    return Breaker(BreakerState(state), failures, window_started_at, opened_at, probe_at)


class PostgresBreakers:
    def __init__(self, database: Database, policy: BreakerPolicy) -> None:
        self._database = database
        self._policy = policy

    def admit(self, key: str, *, now: datetime) -> datetime | None:
        with self._database.connection() as connection:
            current = connection.execute(
                "select state from jobs.circuit_breakers where key = %s", [key]
            ).fetchone()
        if current is None or current[0] == BreakerState.CLOSED.value:
            return None
        return self._change(key, now, lambda b: admit(b, self._policy, now=now))

    def record(self, key: str, *, failed: bool, now: datetime) -> None:
        if not failed:
            with self._database.connection() as connection:
                current = connection.execute(
                    "select state from jobs.circuit_breakers where key = %s", [key]
                ).fetchone()
            if current is None or current[0] != BreakerState.HALF_OPEN.value:
                return  # a success changes only a half-open breaker
        self._change(key, now, lambda b: (record(b, self._policy, failed=failed, now=now), None))

    def _change(self, key: str, now: datetime, step: _Step) -> datetime | None:
        with self._database.connection() as connection, connection.transaction():
            connection.execute(
                "insert into jobs.circuit_breakers (key) values (%s) on conflict do nothing",
                [key],
            )
            row = connection.execute(
                f"select {_BREAKER_COLUMNS} from jobs.circuit_breakers where key = %s for update",  # noqa: S608 - constant columns
                [key],
            ).fetchone()
            breaker = _breaker(row)
            changed, retry_at = step(breaker)
            if changed != breaker:
                connection.execute(
                    "update jobs.circuit_breakers set state = %s, failures = %s, "
                    "window_started_at = %s, opened_at = %s, probe_at = %s, updated_at = %s "
                    "where key = %s",
                    [
                        changed.state.value,
                        changed.failures,
                        changed.window_started_at,
                        changed.opened_at,
                        changed.probe_at,
                        now,
                        key,
                    ],
                )
            return retry_at


class PostgresSlots:
    def __init__(self, database: Database) -> None:
        self._database = database

    def acquire(
        self, key: str, *, limit: int, holder: uuid.UUID, lease_until: datetime, now: datetime
    ) -> datetime | None:
        with self._database.connection() as connection, connection.transaction():
            for slot in range(limit):
                taken = connection.execute(
                    """
                    insert into jobs.concurrency_slots as s (key, slot, holder, lease_until)
                    values (%s, %s, %s, %s)
                    on conflict (key, slot) do update
                      set holder = excluded.holder, lease_until = excluded.lease_until
                      where s.lease_until <= %s
                    returning slot
                    """,
                    [key, slot, holder, lease_until, now],
                ).fetchone()
                if taken is not None:
                    return None
            row = connection.execute(
                "select min(lease_until) from jobs.concurrency_slots where key = %s and slot < %s",
                [key, limit],
            ).fetchone()
        return now if row is None or row[0] is None else row[0]

    def release(self, key: str, *, holder: uuid.UUID) -> None:
        with self._database.connection() as connection:
            connection.execute(
                "delete from jobs.concurrency_slots where key = %s and holder = %s", [key, holder]
            )
