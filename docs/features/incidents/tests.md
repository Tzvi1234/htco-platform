# Incidents — Tests and verification

## Test plan

| Level | What is covered | Location |
|---|---|---|
| Domain | Every allowed and every illegal edge; FR-INC-02's shape; resolution from every open state along legal edges; never without evidence; keys; escalation by severity and age | `tests/domain/test_incidents.py` |
| Application | One incident per key; other causes and orders; a new incident after resolution; resolution path and events; refusals without evidence; no-op without an incident; illegal steps; a person's step; stale writes; drifts | `tests/application/test_incidents.py` |
| Integration (Postgres) | Eight concurrent openings leave one incident and one event; tracking resolves with the evidence in the database; the database refuses resolution without evidence and illegal steps | `tests/integration/test_fulfillment_store.py` |
| Database (pgTAP) | Duplicate open key, illegal update, resolution without evidence, event edges, append-only, a new incident after resolution, members' RLS, the worker's scope and grants | `supabase/tests/database/19_fulfillment_incidents.test.sql` |

## Acceptance criteria → tests

| Criterion | Tests |
|---|---|
| AC-6.3.1 | `test_ac_6_3_1_two_breaches_for_the_same_order_and_cause_open_one_incident`, `test_ac_6_3_1_inspecting_again_opens_nothing_new`, `test_ac_6_3_1_one_dedup_key_per_cause_and_resource`, `test_worker__concurrent_openings_leave_one_open_incident`, pgTAP "a second open incident with the same key is refused" |
| AC-6.3.2 | `test_ac_6_3_2_evidence_resolves_through_verifying_with_the_evidence_recorded`, `test_ac_6_3_2_without_evidence_it_never_resolves`, `test_ac_6_3_2_tracking_resolves_the_open_incident_with_its_evidence`, `test_ac_6_3_2_evidence_resolves_from_every_open_state_along_legal_edges`, `test_ac_6_3_2_without_evidence_an_incident_never_resolves`, `test_worker__tracking_resolves_the_incident_with_evidence_in_the_database`, pgTAP "an incident never resolves without evidence" |
| AC-6.3.3 | `test_ac_6_3_3_every_illegal_transition_is_rejected` (every pair), `test_ac_6_3_3_an_illegal_step_is_rejected_and_nothing_moves`, pgTAP "an illegal step of the incident is refused", "an event outside the machine is refused" |
| AC-6.3.4 | `test_ac_6_3_4_a_drift_opens_one_incident_linked_to_the_order`, `test_ac_6_3_4_a_drift_payload_opens_one_incident` (the `sync_drifts` trigger after #75) |
