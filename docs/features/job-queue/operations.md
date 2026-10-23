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
| `HTCO_DATABASE_URL` | required (secret) | The API's database; the worker runs as `htco_worker` on it |
| `HTCO_WORKER_SCHEDULES` | empty: **no schedule runs** | Comma-separated: `maintenance.capability_probes`, `maintenance.refresh_token_expiry`, `maintenance.purge_disconnected_shops`, `maintenance.prune_finished_jobs`. The feature flag for every scheduled job |
| `HTCO_WORKER_ID` | host name and process ID | Shown in `jobs.worker_heartbeats` and as the lease holder |
| `HTCO_WORKER_LEASE_SECONDS` | 60 | How long a claimed job is held without a heartbeat (beats every third of it) |
| `HTCO_WORKER_POLL_SECONDS` | 1 | Idle wait between polls |
| `HTCO_WORKER_SHUTDOWN_GRACE_SECONDS` | 20 | How long SIGTERM waits for the current job; if blocking database work still holds the loop 5 s later, the process exits with 75 (`worker.forced_exit`) and the job returns after its lease |
| `HTCO_WORKER_ID` (limit) | | At most 200 characters (the queue's columns); longer values stop the worker at startup |
| `HTCO_WORKER_HEALTH_PORT` | 8081 (0 = off) | `GET /healthz`: 200 while the loop runs, 503 when it stalled |
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
3. **Fix:** fix the cause; retrying from the admin view is slice 5.9. Until then an operator may requeue one job with `update jobs.jobs set state = 'queued', attempts = 0, finished_at = null, run_at = now() where id = '<ID>' and state = 'dead'` (record it in the audit notes).
4. **Verify:** the job reaches `succeeded`.

### Jobs are due but nothing runs

1. **Check:** `jobs.worker_heartbeats`: is any `last_beat_at` recent?
2. **Likely cause:** no worker deployed or running, or the worker's kinds do not include the job's kind (its schedule is not enabled).
3. **Fix:** start the worker; enable the schedule in `HTCO_WORKER_SCHEDULES`.

### Jobs keep returning with `LeaseExpired`

1. **Likely cause:** workers are killed (memory, restarts) or a handler blocks longer than the lease without awaiting.
2. **Fix:** check `RESTART_COUNT` and memory; raise `HTCO_WORKER_LEASE_SECONDS` for long jobs.

### A separate login for the worker

To give the worker its own credentials instead of switching roles on the API's login: `alter role htco_worker login password '<generated>'` (owner, SQL editor), store the password as a DigitalOcean secret, and point the worker's `HTCO_DATABASE_URL` at user `htco_worker`. The role's rights do not change.

## Rollback

Remove the schedule from `HTCO_WORKER_SCHEDULES` (or stop the worker): no new jobs run; queued jobs wait. The migration is additive (a schema, two tables, a role, policies and two functions); rolling back the code leaves them unused. Jobs that already ran their effects (probe results, `needs_reauth`, a purge) are not undone; the purge is not reversible except from a backup.

## Drills

| Date | Drill | Result | Follow-ups |
|---|---|---|---|
| 2026-10-22 | Kill-worker test in CI (SIGKILL mid-job, restart) | Automated in `test_worker_killed_mid_job__the_job_completes_exactly_once_after_restart` | The deployed drill is slice 5.8 |
