# Job queue and worker — Tests and verification

## Test plan

| Level | What is covered | Location |
|---|---|---|
| Domain unit / property | Job state machine; dead after the last attempt; jittered backoff stays within half and all of the capped backoff (property); schedule keys equal within a period and the period contains `now` (property) | `tests/domain/test_jobs.py` |
| Application | Job specs, stored errors without buyer data, schedule jobs created once; the daily fan-out (one job per shop and day), probes (GET reads as the scheduler, skipped when not connected or in another organization), expiry check (expired → `needs_reauth` audited, expiring reported), purge (once, audited), pruning | `tests/application/test_jobs.py`, `tests/application/test_maintenance.py` |
| Worker (in memory) | Runs, retries, dead letters, tenant scope, crash release, lease lost, schedules with two workers, three concurrent workers with locks, SIGTERM finishing and grace exceeded, heartbeat extends the lease, poll failures | `tests/worker/test_runner.py` |
| Worker process | Settings and the schedules' feature flag, configuration exit 78, handler wiring, the daily probe job through the real `EtsyAdapter` with an HTTP mock that fails on any non-GET, the health answer | `tests/worker/test_main.py` |
| Integration (Postgres) | Enqueue once; claim, complete; many concurrent claimers; lock keys (sequential and racing); retry and dead letter per organization; expired leases; heartbeats; pruning; schedules with two workers; the worker role (one organization, nothing else, no writes elsewhere, no sessions or identities); what the daily jobs need under the worker role; directory lists; the purge (only that shop, only after its date, not in another scope, not after a reconnect) | `tests/integration/test_job_queue.py` |
| Integration (processes) | A worker process killed with SIGKILL mid-job and a second one completing it exactly once; SIGTERM finishing the job; SIGTERM past the grace period | `tests/integration/test_worker_process.py` (with `worker_harness.py`) |
| Database (pgTAP) | Schema hidden from client roles; RLS on; the role cannot log in or bypass RLS; constraints; one running job per lock key; the worker role's scope | `supabase/tests/database/09_job_queue.test.sql` |
| CLI | `htco admin dead-letters` output, filter, limits, missing configuration | `tests/api/test_cli_dead_letters.py` |
| Application (5.2) | Payload hash, inbox and outbox values, the delivery job: once only, failures recorded and retried, dead on the last attempt, unknown destination | `tests/application/test_messaging.py` |
| API (5.2) | `run_idempotently`: replay without a second effect, another body 422, in progress 409, per user, 24 hours, 5xx and exceptions not stored, key format, fingerprints | `tests/api/test_idempotency.py` |
| Integration (5.2) | Inbox twice and concurrently: one row, one job; outbox once per key with one delivery job; the worker reaches outbox rows of its organization only; the idempotency store (replay, reuse, in progress, expiry, concurrent first requests); a crash after the business commit delivers exactly once | `tests/integration/test_messaging.py`, `tests/integration/test_worker_process.py` |
| Database (5.2) | RLS, no client access, unique inbox events and outbox keys, delivered needs its time, worker scope | `supabase/tests/database/10_inbox_outbox.test.sql` |

## Acceptance criteria → tests

| Criterion | Tests |
|---|---|
| AC-5.1.1 | `test_worker_killed_mid_job__the_job_completes_exactly_once_after_restart`; `test_lease__expired__the_job_is_released_and_the_old_holder_cannot_finish_it`; `test_job_of_a_crashed_worker__is_released_after_its_lease_and_runs_once_more` |
| AC-5.1.2 | `test_claim__many_workers_at_once__never_take_one_job_twice`; `test_lock_key__a_second_job_waits_until_the_first_finished`; `test_lock_key__concurrent_claims__at_most_one_running_per_lock`; `test_two_workers__never_run_one_job_twice_or_one_lock_at_once`; pgTAP "one running job per lock key" |
| AC-5.1.3 | `test_job_failing_every_attempt__ends_dead_with_its_last_error_and_no_buyer_data`; `test_dead_letter__visible_to_its_organization_only_with_the_last_error`; `test_describe_error__*`; `test_dead_letters__prints_one_line_per_dead_job_with_its_last_error` |
| AC-5.1.4 | `test_sigterm__the_current_job_finishes_before_the_worker_exits`; `test_sigterm__a_job_beyond_the_grace_period__is_left_to_its_lease`; `test_stop__lets_the_current_job_finish_then_exits_and_says_so`; `test_stop__a_job_past_the_grace_period__is_abandoned_to_its_lease` |
| AC-5.1.5 | [ADR-0007](../../adr/0007-job-queue.md) status Accepted with the spike table |
| AC-5.1.6 | `test_schedule__two_workers_in_one_period__one_job_runs_once` (Postgres); `test_schedules__two_workers_in_one_period__enqueue_and_run_one_job`; `test_enqueue_capability_probes__twice_on_one_day__one_job_per_shop`; `test_purge__deletes_only_the_disconnected_shops_data_and_only_after_its_date`; `test_purge__in_another_organizations_scope__finds_nothing_to_purge`; `test_reconnect_before_the_purge__keeps_the_shops_data` |
| AC-5.2.1 | `test_inbox__same_event_twice__one_row_and_one_job`; `test_inbox__same_event_at_the_same_time__one_row_and_one_job`; pgTAP `10_inbox_outbox` |
| AC-5.2.2 | `test_crash_after_the_business_commit__the_effect_is_delivered_exactly_once` (real process, SIGKILL); `test_deliver__replayed_job__delivers_once` |
| AC-5.2.3 | `test_same_key_same_body__returns_the_stored_response_without_a_second_effect`; `test_same_key_other_body__is_refused_without_an_effect`; `test_idempotency_store__replays_refuses_and_expires`; `test_idempotency_store__concurrent_first_requests__one_runs` |
| Security checklist: worker role with explicit tenant filters | `test_worker_role__sees_only_the_jobs_organization`; `test_worker_role__cannot_write_another_organizations_rows`; `test_worker_role__has_no_access_to_sessions_identities_or_audit_history`; pgTAP worker-role checks |
| AC-4.5.2 (kept): probes only read | `test_daily_probe_job__reaches_etsy_with_gets_only`; `test_probe_shop_daily__reads_the_shop_and_stores_results_as_the_scheduler` |

## Verification

- Local (Windows, no Docker): lint, types, architecture contracts and every test without a database pass (`uv run just check-fast` equivalents).
- Database and process tests run in CI's `database` job (Supabase CLI, Postgres 17), the only place a database is available for this work; results are in the pull request's checks.
- Not yet verified in a deployed environment: the worker is not deployed until the owner approves its cost. The kill-worker drill in production is slice 5.8.
