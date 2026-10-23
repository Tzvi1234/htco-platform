# Durable automation engine — Tests and verification

## Test plan

| Level | What is covered | Location |
|---|---|---|
| Domain | Execution transitions including the admin retry; the first engine's cases ported to the durable engine | `tests/domain/test_lifecycles.py`, `tests/application/test_automation_engine.py` |
| Application | The durable engine with an in-memory store: replay (also concurrent), refusals logged with reasons, retry within backoff bounds, dead letter, admin retry, crash recovery, the full state-change record, races | `tests/application/test_durable_engine.py` |
| Application | User actions decided by the policy engine (roles, shops, organizations), admin retry, the sync plan, the registry from rows | `tests/application/test_automation_requests.py` |
| API | The routes: list, replayed trigger with `Idempotency-Key`, 400/403/404/409/422/503, executions paging, admin retry | `tests/api/test_automation_routes.py` |
| CLI | `htco registry sync --automations`: dry run, apply idempotently with the commit, check, removed automation, invalid registry | `tests/api/test_cli_registry_sync.py` |
| Integration (Postgres) | The same engine cases on the Postgres store; the database's transition trigger and append-only events; the worker's organization scope; the sync applied twice | `tests/integration/test_durable_engine.py` |
| Integration (process) | A worker process killed during a running execution, then a second one finishing it once | `tests/integration/test_worker_process.py` |
| Database (pgTAP) | RLS and grants, one execution per key, the machine, append-only events, definition references, the worker's scope | `supabase/tests/database/12_automation_engine.test.sql` |
| CI | The definitions load and then match `registry/automations/` (`--apply`, then `--check`) | `.github/workflows/ci.yml`, `database` job |

## Acceptance criteria → tests

| Criterion | Tests |
|---|---|
| AC-5.10.1 | `test_trigger__replayed__one_execution_one_job_and_the_handler_runs_once`; `test_trigger__concurrently__one_execution`; `test_trigger__concurrently_from_many_callers__one_row_one_job_one_run` (Postgres); `test_start__same_idempotency_key__one_execution_and_the_stored_answer` (API); pgTAP |
| AC-5.10.2 | `test_trigger__refused__nothing_written_and_the_reason_logged`; `test_trigger__not_ready__refused_before_any_row` (Postgres); `test_start__an_automation_that_is_not_active__409_and_nothing_written` |
| AC-5.10.3 | `test_trigger_as_user__a_role_without_the_capability__403_and_nothing_written`; `test_trigger_as_user__a_shop_not_granted_or_not_the_organizations__404`; `test_start__a_role_without_the_capability__403`; `test_start__a_shop_the_user_may_not_use__404` |
| AC-5.10.4 | `test_failing_handler__retries_within_the_backoff_bounds_then_dead_letters`; `test_admin_retry__reuses_the_dead_lettered_execution_and_is_audited`; `test_failing_handler__retry_dead_letter_admin_retry_all_recorded` (Postgres); `test_retry__an_admin_sends_a_dead_letter_back_once` |
| AC-5.10.5 | `test_worker_killed_during_a_running_execution__one_success_and_one_effect` (real process, SIGKILL); `test_run_left_running_by_a_dead_worker__succeeds_once` (Postgres); `test_worker_died_while_running__the_next_run_succeeds_once` |
| AC-5.10.6 | `test_every_state_change__is_recorded_with_its_actor_and_correlation`; `test_database__refuses_a_transition_outside_the_machine_and_event_edits`; pgTAP |
| AC-5.10.7 | `test_sync__dry_run_shows_every_definition_and_writes_nothing`; `test_sync__apply_is_idempotent_and_records_the_commit`; `test_sync__check_fails_while_the_database_differs`; `test_registry_sync__applied_twice__the_second_plan_is_empty` (Postgres); CI step |

## Verification

- Local (Windows, no Docker): lint, types, architecture contracts, the OpenAPI snapshot and every test without a database.
- Database, pgTAP, process and sync tests: CI's `database` job.
- Not exercised in a deployed environment: no automation is ready and the worker is not deployed.
