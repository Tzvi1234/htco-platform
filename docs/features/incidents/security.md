# Incidents — Security

| Threat | Control |
|---|---|
| Buyer data in incidents or their events | An incident names its order by shop and receipt number only; `details` are identifiers and figures (4 kB cap); notes are reason codes (`^[a-z0-9_.:-]{1,64}$`); evidence references are IDs |
| A false "resolved" | Resolution needs evidence (domain, service, database trigger, check constraint); `move` refuses `RESOLVED` |
| Tampering with history | `incident_events` is append-only (trigger); the worker can update only `state`, `severity`, `updated_at` and `resolved_at` of an incident |
| Cross-tenant access | RLS: members read their organization's incidents of their shops (organization-wide ones without a shop); the worker only its job's organization; members cannot write incidents directly (the API's own endpoints arrive with 6.5); a drift run opens an incident only for the execution's own shop, and an execution without a shop is refused, so the guard fails closed (`test_a_drift_for_another_shop__is_refused_and_opens_nothing`) |

ASVS: V4.1, V7.1, V11.1 (business logic limits: one incident per key).
