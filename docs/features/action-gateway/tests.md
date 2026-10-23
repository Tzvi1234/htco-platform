# Action Gateway — Tests

| Criterion | Tests |
|---|---|
| AC-5.12.1 | `tests/application/test_action_gateway.py::test_submit__twice__runs_once_and_returns_the_first_result`, `::test_submit__concurrently__one_runs_the_other_is_deferred_then_gets_the_result`; `tests/integration/test_action_store.py::test_claim__concurrently__one_row_and_one_winner`, `::test_submit__runs_once_records_audit_and_event_and_keeps_no_r1_output`; pgTAP `13` |
| AC-5.12.2 | `test_submit__refused_before_any_channel_with_the_reason_recorded` (6 cases), `test_submit__user_role_and_shop__decided_by_the_policy_engine`, `test_submit__a_role_without_the_capability__forbidden`, `test_denied__recorded_with_its_reason` (Postgres) |
| AC-5.12.3 | `test_write__without_approval__pending_and_nothing_runs_then_runs_once_approved`, `test_write__the_approval_is_checked_again_at_execution_time`, `test_write__an_approval_for_another_request__is_refused`, `test_write__the_owners_own_trigger__runs_without_an_approval` |
| AC-5.12.4 | `test_result__one_row_an_audit_for_personal_data_and_an_event_without_secrets` (log capture), `test_result__an_r0_read__has_no_audit_record`, the Postgres submit test |
| AC-5.12.5 | `test_scenario_h__an_automation_outside_its_permissions__denied`, `test_scenario_h__an_employee_outside_its_grants__denied` |
| Contracts | `tests/application/test_action_contracts.py` (machine, schema checker, catalog), `tests/persistence/test_schema_mirrors_domain.py` |

Verification: local lint, types, architecture and every non-database test; database and pgTAP tests in CI.
