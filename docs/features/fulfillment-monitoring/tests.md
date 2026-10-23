# Fulfillment monitoring — Tests and verification

## Test plan

| Level | What is covered | Location |
|---|---|---|
| Domain | Tracking formats per carrier, clear refusals, no code in a repr, carrier names, fingerprints; business days (weekend, holidays, weekend start, other weekends and zones, never on a weekend: property); SLA deadlines at the boundary; the most specific policy; the default; ship-by warning windows | `tests/domain/test_fulfillment.py` |
| Application | The handler through the real gateway and API channel: two shipments once over six replays, GET only; first sighting, source and trigger; the open incident resolved with evidence; no shipments; empty codes; the schedule; disconnected; bad payloads; the webhook trigger and its refusal | `tests/application/test_tracking_sync.py`, `tests/application/test_tracking_webhooks.py` |
| Application | The inspector: deadline boundary, business days, repeated inspections, cancellation, escalation once, drifts, bad payloads; the schedule and its per-shop job | `tests/application/test_sla_inspector.py` |
| Contracts | The automation schemas use only the gateway checker's keywords; real payloads validate; buyer data does not fit | `tests/application/test_fulfillment_contracts.py` |
| Worker | Handlers deployed with and without the Etsy vault; the schedule off unless named; settings accept it | `tests/worker/test_fulfillment_wiring.py` |
| Integration (Postgres) | As `htco_worker`: each parcel once with its event once; the inspector opens one incident for a late order and none for a recent one | `tests/integration/test_fulfillment_store.py` |
| Database (pgTAP) | One row per parcel, last four only, the worker's grants and organization scope | `supabase/tests/database/19_fulfillment_incidents.test.sql` |

## Acceptance criteria → tests

| Criterion | Tests |
|---|---|
| AC-6.1.1 | `test_ac_6_1_1_each_shipment_once_however_often_it_is_observed`, `test_ac_6_1_1_fingerprint_is_one_per_parcel_and_shop`, `test_worker__each_parcel_once_and_its_event_once`, pgTAP "a parcel is recorded once per order" |
| AC-6.1.2 | `test_ac_6_1_2_the_first_sighting_keeps_its_time_source_and_trigger` (the timeline view itself arrives with the Orders UI, 5.7) |
| AC-6.2.1 | `test_ac_6_2_1_an_order_at_exactly_its_deadline_is_not_breached`, `test_ac_6_2_1_business_day_boundary`, `test_ac_6_2_1_at_exactly_the_deadline_no_breach_one_microsecond_later_one` |
| AC-6.2.2 | `test_ac_6_2_2_business_days_skip_the_weekend`, `..._skip_configured_holidays`, `..._an_order_paid_on_a_weekend_counts_from_the_next_business_day`, `test_ac_6_2_2_business_day_policies_wait_over_the_weekend` |
| AC-6.7.2 (part) | `test_order_shipped_starts_intake_and_tracking_sync_once_each`; the full replay test arrives with 6.7 |

## Real verification

Not yet: both automations are `draft`. The phase's demo (a synthetic late order in a test shop) runs at activation (6.7).
