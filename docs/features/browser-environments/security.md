# Browser Environments — Security

| Threat | Control | Test |
|---|---|---|
| Disguising who acts or from where | No fingerprint, user-agent or proxy fields at all (ADR-0020) | `test_environment__has_no_fingerprint_or_proxy_field`, pgTAP `hasnt_column` |
| A browser action on an unhealthy or unconfigured shop | Only `READY` accepts browser actions; every flag is off until 8A | `test_gate__browser_binding__needs_ready_and_stays_off_until_8a` |
| A status moved outside the table, or history rewritten | Domain check, database trigger, check constraint on the history, append-only trigger | AC-5.16.1 and AC-5.16.2 tests, pgTAP `16` |
| Cross-tenant reads or changes | RLS (Owner and Admin of the organization; the worker by its job's organization); users cannot write; 404 for another organization's shop | pgTAP `16`, `test_get__another_organizations_shop__404` |
| Consent or network details exposed | The consent keeps a SHA-256 of the address only and is not readable by users; health problems are codes, never free text; the event carries IDs and statuses | pgTAP `16`, `test_status_event__carries_ids_and_statuses_only`, `test_health_report__problems_are_codes` |
| An environment created early | The registry refuses while the `browser_environments` flag is off; there is no creation endpoint | `test_create__flag_off__is_refused_and_writes_nothing`, `test_create__there_is_no_endpoint` |

ASVS v5.0.0-8.2.1, 8.2.2, 8.3.1, 16.3.x.
