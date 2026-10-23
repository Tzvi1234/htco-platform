# Action Gateway — Tests

| Criterion | Tests |
|---|---|
| AC-5.12.1 | `tests/application/test_action_gateway.py::test_submit__twice__runs_once_and_returns_the_first_result`, `::test_submit__concurrently__one_runs_the_other_is_deferred_then_gets_the_result`; `tests/integration/test_action_store.py::test_claim__concurrently__one_row_and_one_winner`, `::test_submit__runs_once_records_audit_and_event_and_keeps_no_r1_output`; pgTAP `13` |
| AC-5.12.2 | `test_submit__refused_before_any_channel_with_the_reason_recorded` (6 cases), `test_submit__user_role_and_shop__decided_by_the_policy_engine`, `test_submit__a_role_without_the_capability__forbidden`, `test_denied__recorded_with_its_reason` (Postgres) |
| AC-5.12.3 | `test_write__without_approval__pending_and_nothing_runs_then_runs_once_approved`, `test_write__the_approval_is_checked_again_at_execution_time`, `test_write__an_approval_for_another_request__is_refused`, `test_write__the_owners_own_trigger__runs_without_an_approval` |
| AC-5.12.4 | `test_result__one_row_an_audit_for_personal_data_and_an_event_without_secrets` (log capture), `test_result__an_r0_read__has_no_audit_record`, the Postgres submit test |
| AC-5.12.5 | `test_scenario_h__an_automation_outside_its_permissions__denied`, `test_scenario_h__an_employee_outside_its_grants__denied` |
| AC-5.15.1 | `tests/application/test_action_guards.py::test_two_workers_four_shops__never_above_the_qps_or_the_qpd`; `tests/integration/test_action_guards_store.py::test_two_workers_four_shops__never_above_the_qps`, `::test_two_workers_four_shops__never_above_the_qpd` |
| AC-5.15.2 | `tests/domain/test_guards.py` (open, cooldown, probe), `test_five_failures__open_the_breaker__deferred_then_one_probe_closes_it`, `test_half_open__only_one_call_goes_through_at_a_time`, `test_breaker__shared_by_the_workers__one_probe_after_the_cooldown` (Postgres) |
| AC-5.15.3 | `test_a_4xx__never_retried_and_not_a_breaker_failure`, `test_a_429__waits_for_retry_after_then_runs`, `test_a_429_with_a_long_or_unknown_wait__deferred_until_then`, `test_a_5xx_write__never_retried_inside_the_attempt` |
| AC-5.15.4 | `test_two_writes_to_one_shop__never_at_the_same_time`, `test_writes_to_two_shops__run_side_by_side`, `test_slots__one_holder_at_a_time_across_workers` (Postgres); pgTAP `14` |
| Contracts | `tests/application/test_action_contracts.py` (machine, schema checker, catalog), `tests/persistence/test_schema_mirrors_domain.py` |

Verification: local lint, types, architecture and every non-database test; database and pgTAP tests in CI.
