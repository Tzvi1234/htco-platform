# Living office — Tests and verification

## Test plan

| Level | What is covered | Location |
|---|---|---|
| Domain | Identities, profiles and the readiness gate; routing policies; tools; automation definitions, retry backoff and the execution state machine; tasks; activity records; optimization proposals | `tests/domain/` |
| Application | Registry parsing and cross-checks; tool access (Scenario H); automation engine (Scenarios G and H); router (Scenario E); decision advisor (Scenario F); framework independence (Scenario J) | `tests/application/test_registry.py`, `test_tool_access.py`, `test_automation_engine.py`, `test_model_router.py`, `test_decision_advisor.py`, `test_framework_independence.py` |
| Data | The committed office: 16 employees in 7 departments, Jev, 15 automations, deterministic automations, agent-tool grants, nothing production-ready, one routing policy per employee, activation phases | `tests/tools/test_registry_files.py` |

## Acceptance criteria coverage

| Criterion | Tests |
|---|---|
| AC-LO.1 | `tests/tools/test_registry_files.py` |
| AC-LO.2 | `test_profile__production_ready_without_a_passed_evaluation__is_rejected`, `test_automation__ready_with_draft_sections__is_rejected`, `test_nothing__is_production_ready_by_configuration_alone` |
| AC-LO.3 | `@ac("SCN-H")` tests |
| AC-LO.4 | `@ac("SCN-G")` and the retry and dead-letter tests |
| AC-LO.5 | `@ac("SCN-E")` tests |
| AC-LO.6 | `@ac("SCN-F")` tests |
| AC-LO.7 | `@ac("SCN-J")` tests |

## Verification evidence

### Verification — foundation — 2026-10-14

| Criterion | Covered by tests | Verified for real | Result |
|---|---|---|---|
| AC-LO.1 | Registry file tests | `uv run just registry`: "16 employees (0 production-ready), 15 automations, 40 tools, 1 advisor(s)". The tests caught a real inconsistency during the build (employees granted automations they could not trigger); fixed by a registry rule and the data. | Pass |
| AC-LO.2–7 | 136 new domain and application tests | Red first for every module (collection errors, then failing tests); a router bug (fallback below the task's difficulty) found by a new test and fixed. `uv run just check`: 705 tests, domain 100% and application 98% branch coverage, architecture contracts kept. | Pass |

**Open issues:** the end-to-end versions of scenarios A–D, G, H and I are scheduled in their phases ([TESTING_STRATEGY §10](../../engineering/TESTING_STRATEGY.md#10-office-acceptance-scenarios)).
