# Action Gateway — Security

| Threat | Control | Test |
|---|---|---|
| An actor acts beyond its rights | One gate per actor kind (policy engine, tool grants, `allowed_actions`, R0 for the platform, advisors never); the actor comes from the authenticated context or the execution | `test_scenario_h__*`, `test_submit__user_role_and_shop__*` |
| A write without consent | W1 to W3 need an approval for exactly this request (SHA-256 of everything sent) or the Owner's own trigger | `test_write__*` |
| Replays and races | One row per organization, action and key; concurrent submissions defer | AC-5.12.1 tests, pgTAP |
| Buyer data and secrets at rest or in logs | Reasons are codes and short texts; R1 outputs never stored; events carry IDs only; the schema checker never echoes values | `test_result__one_row_an_audit_for_personal_data_and_an_event_without_secrets`, Postgres test |
| Cross-tenant access | RLS for users and the worker; users never read stored outputs | pgTAP `13` |
| Live shop | No Etsy channel is deployed by this slice; Etsy writes stay off until Phases 6 and 7 | — |

ASVS v5.0.0-8.2.1, 8.2.2, 8.3.1, 15.4.x, 16.3.x.
