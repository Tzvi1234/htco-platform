# Durable automation engine — Operations

## Configuration

| Setting / flag | Default | Effect |
|---|---|---|
| `HTCO_AUTOMATIONS_ENABLED` (API) | `false` | Turns on `POST /v1/automations/{id}/executions` and the admin retry; otherwise they answer 503 `automations-unavailable`. Reading works whenever the database is configured. Enable only with a running `agent-worker` |
| Registry `readiness` | `draft` for every automation | Only `ready` or `active` automations with a deployed handler can be triggered |
| Registry `user_capability` | absent | The permission-matrix capability a user needs to start the automation; absent means no user may |

## Loading definitions

The migration workflow runs `htco registry sync --automations` after the migrations: a dry run always (one JSON line per added or changed definition and a summary), and `--apply` with `apply: true`. Locally, with `HTCO_DATABASE_URL`:

```bash
uv run htco registry sync --automations            # the plan
uv run htco registry sync --automations --apply    # write it (idempotent)
uv run htco registry sync --automations --check    # exit 1 while anything differs
```

A definition whose file was removed blocks the sync (exit 65): keep the file and set `readiness = "retired"`.

## Metrics and alerts

Execution metrics (count, success rate, retries, dead letters, latency p50/p95) and alerts arrive with slice 5.9. Until then:

```sql
select automation_id, state, count(*) from app.automation_executions group by 1, 2;
select * from app.automation_execution_events where execution_id = '<ID>' order by occurred_at;
```

Log events: `automation.refused` (with `reason`, `trigger`, `actor`, `correlation_id`), and the job events of the [job queue](../job-queue/operations.md).

## Runbook

### An execution is dead-lettered

1. **Check:** `GET /v1/automations/{id}/executions` (or the SQL above): `last_error` and the events.
2. **Fix** the cause (a dependency, the handler).
3. **Retry:** an Owner or Admin calls `POST /v1/automations/{id}/executions/{execution_id}/retry`; the same execution gets one more attempt (audited).

### Triggers answer 409 `automation-refused`

The automation is not `ready`, has no deployed handler, or is not callable by users: check its registry file and the `automation.refused` log line's reason.

## Rollback

Set the automation's `readiness` back to `draft` in its file, merge, and run the sync: new triggers are refused; executions already queued still run (their handler is deployed). Turning `HTCO_AUTOMATIONS_ENABLED` off stops user triggers. The migration is additive.

## Drills

| Date | Drill | Result | Follow-ups |
|---|---|---|---|
| 2026-10-22 | Kill the worker during a running execution (CI) | Automated: `test_worker_killed_during_a_running_execution__one_success_and_one_effect` | The deployed drill is slice 5.8 |
