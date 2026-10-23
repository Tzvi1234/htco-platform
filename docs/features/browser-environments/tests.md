# Browser Environments — Tests

| Criterion | Tests |
|---|---|
| AC-5.16.1 | `tests/domain/test_browser_environment.py::test_transition__allowed_edge__succeeds` (20 edges), `::test_transition__any_other_edge__is_refused` (29 pairs), `::test_transition__any_walk__stays_inside_the_table` (Hypothesis); `tests/application/test_browser_environments.py::test_transition__the_happy_path__walks_the_table`, `::test_transition__illegal_edge__is_refused_and_records_nothing`; `tests/persistence/test_schema_mirrors_domain.py::test_browser_environment_edges__the_database_has_exactly_the_domain_table`; `tests/integration/test_browser_environment_store.py::test_database__refuses_an_edge_outside_the_table`; pgTAP `16` |
| AC-5.16.2 | `test_change__records_actor_reason_and_correlation`, `test_change__needs_a_reason_and_a_correlation`, `test_create__provisions_and_records_the_first_change`, `test_every_change__has_actor_reason_correlation_and_one_event`; `test_transitions__store_history_and_one_event_each` (Postgres); pgTAP `16` (append-only, edges only) |
| AC-5.16.3 | pgTAP `16` (Owner E sees and changes nothing of F; Support sees nothing; the worker its organization only); `test_reader__an_owner_sees_their_shop_only` (Postgres); `test_status__another_organizations_or_unknown_shop__is_not_found`; `tests/api/test_browser_environment_routes.py::test_get__another_organizations_shop__404` |
| AC-5.16.4 | `test_create__flag_off__is_refused_and_writes_nothing`, `test_shop_without_environment__is_not_configured`, `test_gate__without_environment__api_actions_route_unchanged`, `test_gate__browser_binding__needs_ready_and_stays_off_until_8a`, `test_get__shop_without_environment__not_configured`, `test_create__there_is_no_endpoint` |
| Honest design | `test_environment__has_no_fingerprint_or_proxy_field`; pgTAP `16` (`hasnt_column` proxy, fingerprint) |

Verification: local lint, types, architecture and every non-database test; database and pgTAP tests in CI.
