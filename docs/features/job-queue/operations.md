# Job queue and worker — Operations

## Running the worker

```bash
# Needs HTCO_DATABASE_URL (the same database as the API) and HTCO_ENVIRONMENT.
uv run python -m htco_worker
```

The process logs `worker.starting` with its ID and enabled schedules, serves `GET /healthz` on `HTCO_WORKER_HEALTH_PORT`, and stops on SIGTERM or SIGINT (`worker.signal`, then `worker.stopped`). Its connections switch to the `htco_worker` role, which the migration creates and grants to `postgres`.

## Configuration

| Setting / flag | Default | Effect |
|---|---|---|
| `HTCO_ENVIRONMENT` | required | `local`, `test`, `staging` or `production` |
| `HTCO_DATABASE_URL` | required (secret) | The API's database; the worker runs as `htco_worker` on it. Only the fallback login once the worker has its own (below) |
| `HTCO_WORKER_DATABASE_URL`, `HTCO_WORKER_DATABASE_PASSWORD` | unset (password: secret) | The worker's own login `htco_worker_login` (audit 5.1): used when both are set, checked at startup (exit 78 on the wrong login). Unset or blank: the API's login, with a `worker.shared_database_login` WARNING. See "The worker's own login" |
| `HTCO_WORKER_SCHEDULES` | empty: **no schedule runs** | Comma-separated: `maintenance.capability_probes`, `maintenance.refresh_token_expiry`, `maintenance.purge_disconnected_shops`, `maintenance.prune_finished_jobs`. The feature flag for every scheduled job |
| `HTCO_WORKER_ID` | host name and process ID | Shown in `jobs.worker_heartbeats` and as the lease holder |
| `HTCO_WORKER_LEASE_SECONDS` | 60 | How long a claimed job is held without a heartbeat (beats every third of it) |
| `HTCO_WORKER_POLL_SECONDS` | 1 | Idle wait between polls |
| `HTCO_WORKER_SHUTDOWN_GRACE_SECONDS` | 20 | How long SIGTERM waits for the current job; if blocking database work still holds the loop 5 s later, the process exits with 75 (`worker.forced_exit`) and the job returns after its lease |
| `HTCO_WORKER_ID` (limit) | | At most 200 characters (the queue's columns); longer values stop the worker at startup |
| `HTCO_WORKER_HEALTH_PORT` | 8081 (0 = off) | `GET /healthz`: 200 while the loop runs, 503 when it stalled |
| `HTCO_SENTRY_DSN` | unset (secret; blank = off) | A dead-lettered automation execution is reported to Sentry as `DeadLetteredExecutionError`, with the automation ID, execution ID and stored error only (AC-6.7.4), tagged `automation_id`, `error_type` and `execution_id`; one Sentry issue per dead letter (no fingerprint), so the "new issue" alert fires for each; closed at shutdown so queued reports are sent |
| `HTCO_TOKEN_VAULT_KEY`, `HTCO_TOKEN_VAULT_PREVIOUS_KEYS` | unset | Needed by the probes and the expiry check (startup fails otherwise) |
| `HTCO_ETSY_KEYSTRING`, `HTCO_ETSY_SHARED_SECRET`, `HTCO_ETSY_DAILY_QUOTA_FLOOR` | unset, 500 | Needed by the probes |

Nothing breaks without a worker: the API enqueues no job in slice 5.1, and the schedules exist only in the worker.

## Deployment

The `agent-worker` App Platform component (`workers:` in `infra/digitalocean/app.yaml`, approved by the owner on 2026-10-23) runs the API's image with `python -m htco_worker` on one fixed `apps-s-1vcpu-0.5gb` instance: **$5 per month**. It runs the automation, API and Internal channels; the `browser-worker` is a separate component of Phase 8A with its own cost line, never part of `agent-worker`. It uses the API's database URL and password, the token vault keys and the Etsy app credentials, and enables the four maintenance schedules. The health check on port 8081 is not yet confirmed for App Platform workers; the deploy's cost proposal validates the spec before anything changes.

**Cost limit.** The deploy workflow refuses a spec whose monthly cost, as App Platform's proposal states it, is above **$20** (the owner's hard limit of $15–20; today the API and the worker are $5 each, $10 in total). What enforces what:

- *Enforced by us, before a deploy:* the budget step (`/v2/apps/propose`) fails the deploy above $20, so no change to the spec can raise the bill past it unnoticed.
- *Enforced by the spec:* one fixed-size instance per component, no `autoscaling` block, so App Platform never adds instances by itself.
- *Not enforced by DigitalOcean:* App Platform has no spending cap that stops or throttles an app. Usage outside the instance price (for example outbound transfer beyond the plan's allowance) is billed as it happens, and an account-level billing alert only notifies. The owner sets that alert in the DigitalOcean control panel (Billing → billing alerts) at $20.

## Metrics and alerts

Queue depth and age, retries, dead-letter size and heartbeat age become metrics and alerts in slice 5.9. Until then:

```sql
-- Depth and age of due work
select kind, count(*), min(run_at) from jobs.jobs
where state in ('queued', 'retry_scheduled') and run_at <= now() group by kind;
-- Workers and their last beat
select worker_id, release, last_beat_at, current_job_id, stopping from jobs.worker_heartbeats
order by last_beat_at desc;
```

Log events: `job.started`, `job.succeeded`, `job.retry_scheduled`, `job.dead_lettered`, `job.lease_lost`, `jobs.leases_expired`, `worker.poll_failed`, `worker.heartbeat_failed`, `worker.grace_exceeded`, `etsy.refresh_token_expiring`, `shop.purged`.

## Runbook

### A job is dead-lettered

1. **Check:** `htco admin dead-letters [--organization <ID>]` (needs `HTCO_DATABASE_URL`) shows its kind, attempts and last error.
2. **Likely cause:** a dependency down for longer than the retries (Etsy, the database), or a bug in the handler.
3. **Fix:** fix the cause, then retry it from the admin screen (Admin › Operations, slice 5.9), or call `POST /v1/operations/dead-letters/{job_id}/retry` as the Owner or an Admin. The job is queued again with attempts from zero, due now, and the audit records `job.dead_letter_retried` with its kind and last error. A job of the engine (`automation.execute`) answers `409 retry-through-execution` with its `execution_id`: retry that execution instead (`POST /v1/automations/{automation_id}/executions/{execution_id}/retry`).
4. **Verify:** the job reaches `succeeded`.

### Jobs are due but nothing runs

1. **Check:** the admin screen's Workers panel (`GET /v1/operations/health`, `workers[]`: `alive` is false once the last beat is older than two minutes), or `jobs.worker_heartbeats`: is any `last_beat_at` recent?
2. **Likely cause:** no worker deployed or running, or the worker's kinds do not include the job's kind (its schedule is not enabled).
3. **Fix:** start the worker; enable the schedule in `HTCO_WORKER_SCHEDULES`.

### Jobs keep returning with `LeaseExpired`

1. **Likely cause:** workers are killed (memory, restarts) or a handler blocks longer than the lease without awaiting.
2. **Fix:** check `RESTART_COUNT` and memory; raise `HTCO_WORKER_LEASE_SECONDS` for long jobs.

### The worker's own login (architecture audit 5.1, ER-SEC-08)

The migration `20261028210000_worker_login_role.sql` creates `htco_worker_login`: a LOGIN role, NOINHERIT, no superuser, no BYPASSRLS, no member of `postgres` or of a client role, whose only grant is the switch to `htco_worker` (so it reaches exactly what the worker role reaches, and a `reset role` lands on a role that reaches nothing). It has **no password** in the repository, so it cannot log in until the owner sets one. Until then the worker keeps the fallback: it logs in with the API's login (`HTCO_DATABASE_URL` and `PGPASSWORD`) and logs `worker.shared_database_login` at WARNING on every start.

Owner steps, once, before or right after the first cloud deploy of this migration:

1. **Generate a password** locally, letters and digits only, for example `python -c "import secrets; print(secrets.token_hex(32))"`. Do not paste it anywhere but the two places below.
2. **Set it in Supabase** (Dashboard › SQL Editor, as `postgres`):
   ```sql
   alter role htco_worker_login password '<generated>';
   ```
3. **Store it as a GitHub secret**: Settings › Secrets and variables › Actions › New repository secret, name `HTCO_WORKER_DATABASE_PASSWORD`. The deploy workflow maps it into the app spec; App Platform keeps it as an encrypted `SECRET` env of the `agent-worker` only.
4. **Deploy** (Actions › Deploy API). The spec already wires both settings of the worker:
   ```yaml
   - key: HTCO_WORKER_DATABASE_URL
     scope: RUN_TIME
     value: postgresql://htco_worker_login.${SUPABASE_PROJECT_REF}@${SUPABASE_POOLER_HOST}:5432/postgres?sslmode=require
   - key: HTCO_WORKER_DATABASE_PASSWORD
     scope: RUN_TIME
     type: SECRET
     value: ${HTCO_WORKER_DATABASE_PASSWORD}
   ```
   The URL never holds the password; libpq takes it from the separate setting.
5. **Verify**: the worker's runtime log shows `worker.database_login` with `session_user=htco_worker_login` and `current_user=htco_worker`, and no `worker.shared_database_login` warning. In Supabase, `select usename from pg_stat_activity where usename = 'htco_worker_login'` lists its connections.
6. **Then** remove `PGPASSWORD` from the `agent-worker` envs in `infra/digitalocean/app.yaml` (a small PR), so the worker no longer holds the API's password at all.

At startup, when both `HTCO_WORKER_DATABASE_URL` and `HTCO_WORKER_DATABASE_PASSWORD` are set, the worker checks `session_user = htco_worker_login`, `current_user = htco_worker` and that the login is neither superuser nor BYPASSRLS; otherwise it logs `worker.database_login_refused` and exits 78 (configuration error) before running any job. A password without the URL is refused at startup too.

**Rotation:** `alter role htco_worker_login password '<new>'`, update the GitHub secret, redeploy. Connections opened with the old password stay until they close; new ones fail until the deploy finishes, and the worker's jobs wait in the queue meanwhile. **Rollback:** delete the GitHub secret and redeploy: the worker falls back to the API's login (with the warning).

## Rollback

Remove the schedule from `HTCO_WORKER_SCHEDULES` (or stop the worker): no new jobs run; queued jobs wait. The migration is additive (a schema, two tables, a role, policies and two functions); rolling back the code leaves them unused. Jobs that already ran their effects (probe results, `needs_reauth`, a purge) are not undone; the purge is not reversible except from a backup.

## Drills

| Date | Drill | Result | Follow-ups |
|---|---|---|---|
| 2026-10-22 | Kill-worker test in CI (SIGKILL mid-job, restart) | Automated in `test_worker_killed_mid_job__the_job_completes_exactly_once_after_restart` | The deployed drill is slice 5.8 |
