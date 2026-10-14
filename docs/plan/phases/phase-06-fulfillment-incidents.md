# Phase 6 — Fulfillment monitoring and incidents

| | |
|---|---|
| **Milestone** | — |
| **Repositories** | both |
| **Size** | M |
| **Depends on** | Phase 5 |
| **Requirements** | FR-AUT-05, FR-EML-01, FR-INC-01, FR-INC-02, FR-INC-03, FR-INC-04 (allowlist only), FR-INC-05, FR-INC-06, FR-NTF-01, FR-ORD-03, FR-ORD-05, FR-ORD-06, FR-ORD-07 |
| **Status** | Not started |

## Progress

- [ ] 6.1 Tracking detection
- [ ] 6.2 SLA policies and inspector
- [ ] 6.3 Incidents
- [ ] 6.4 Notifications and email
- [ ] 6.5 Incident center UI
- [ ] 6.6 Guarded shipment write
- [ ] 6.7 Fulfillment automations activated

Slices 6.1 to 6.4 are built as two automations on the engine of [Phase 5](phase-05-orders-ingest.md): **Tracking Synchronization** (slice 6.1) and **Incident Detection & Escalation** (slices 6.2 to 6.4). Slice 6.7 completes their specifications and activates them.

## Goal

HTCO notices fulfillment problems before buyers do: when an order has no tracking after its realistic window, exactly one incident opens and the owner gets exactly one email; when tracking appears in Etsy, the incident closes itself on that evidence. All deterministic code, no AI: routine fulfillment processing is done by automations, not by an employee. Eitan, the Fulfillment Inspector, joins in [Phase 9](phase-09-router-advisor.md) to investigate the exceptions these automations raise.

## Scope

**In:** tracking detection from shipment records and `order.shipped` webhooks, SLA policies, the inspector job, incidents with state machine and deduplication, in-app notifications, transactional email through Resend from `noreply@office.ht-co.app`, incident center UI, guarded shipment write (only if the capability is verified).

**Out:** approvals for remediations ([Phase 7](phase-07-tasks-approvals.md)); exception investigation by Eitan ([Phase 9](phase-09-router-advisor.md)); incident remediation by Amir, the Debugger & Reliability Engineer ([Phase 19](phase-19-engineering.md)). Until Phase 19, remediation is either allowlisted, reversible, idempotent code (for example re-running a sync, FR-INC-04) or a person.

## Automations activated

| Automation | Registry | Slices | Business owner |
|---|---|---|---|
| Tracking Synchronization | [`tracking-sync.toml`](../../../registry/automations/tracking-sync.toml) | 6.1 | Eitan (the owner is accountable until Eitan is active in Phase 9) |
| Incident Detection & Escalation | [`incident-detection-escalation.toml`](../../../registry/automations/incident-detection-escalation.toml) | 6.2, 6.3, 6.4 | Amir (the owner is accountable until Amir is active in Phase 19) |

The activation rule of Phase 5 applies: all 18 specification sections defined, schemas in `packages/contracts/automations/`, replay, failure and recovery tests passing, a runbook, and the owner's approval.

## Who handles an incident, by phase

| From | Detection | Investigation | Remediation | Resolution |
|---|---|---|---|---|
| Phase 6 | Incident Detection & Escalation (deterministic) | A person | Allowlisted code or a person | On evidence only (FR-INC-05) |
| Phase 9 | Same | Eitan for fulfillment exceptions | Allowlisted code or a person; non-allowlisted fixes become approvals | Same |
| Phase 10 | Same, plus Noa's monitors and the nightly supervisor | Eitan; Noa follows up | Same | Same, verified by Noa's follow-up |
| Phase 19 | Same | Same | Amir for technical incidents, within the allowlist; everything else through approvals or engineering requests | Same |

## Owner prerequisites

- DNS for `office.ht-co.app`: SPF, DKIM and DMARC records for Resend (no MX yet).
- Resend account; sending domain verified.
- Agreement on initial SLA windows (from the Phase 0 data).

## Slices

### 6.1 — Tracking detection

Shipments from ingest become `tracking_events`; the order moves to `SHIPPED` with its tracking code recorded, plus the source and time it was observed; `fulfillment.tracking_detected.v1`.

| ID | Acceptance criterion |
|---|---|
| AC-6.1.1 | A receipt with multiple shipments records each once; repeated observation adds no duplicates. |
| AC-6.1.2 | The order timeline shows when tracking first appeared and from which source. |

### 6.2 — SLA policies and inspector

`sla_policies` per shop, supplier and shipping method, with business-day calendars; defaults from Phase 0 measurements; scheduled inspector emits `fulfillment.sla_breached.v1` for orders past their window without tracking.

| ID | Acceptance criterion |
|---|---|
| AC-6.2.1 | An order inside its window never produces a breach (boundary tests at exactly the deadline). |
| AC-6.2.2 | Weekends and configured holidays are excluded when the policy says business days. |

### 6.3 — Incidents

Incident aggregate and state machine; deduplication (one open incident per resource and cause); automatic transition to `VERIFYING` and `RESOLVED` when evidence arrives; `incidents.opened.v1` and `incidents.resolved.v1`.

| ID | Acceptance criterion |
|---|---|
| AC-6.3.1 | Two breaches for the same order and cause produce one incident. |
| AC-6.3.2 | When tracking appears, the incident resolves with the evidence recorded; without evidence, it never resolves. |
| AC-6.3.3 | Every illegal state transition is rejected (state-machine tests). |
| AC-6.3.4 | A drift recorded by reconciliation (Phase 5) opens one incident linked to the order. |

### 6.4 — Notifications and email

`notifications` (in-app) and `notification_preferences`; `EmailPort` with the Resend adapter; emails sent through the outbox with an idempotency key; templates in Hebrew and English; `Reply-To` set to the support address. Security notifications by email: sign-in from a new device, MFA changes, shop connected or disconnected, integration needs re-authentication, refresh token close to expiry.

| ID | Acceptance criterion |
|---|---|
| AC-6.4.1 | One incident produces one email even if the delivery job runs twice. |
| AC-6.4.2 | Email content contains no buyer PII beyond the receipt number. |
| AC-6.4.3 | Resend failures retry with backoff; permanent failures are visible in the admin view. |
| AC-6.4.4 | A sign-in from a new device sends one email to that user; MFA changes and shop connections notify the owner. |

### 6.5 — Incident center UI

Incident list (severity, shop, age, state), incident detail (evidence, timeline, related order), notification bell; links from the order timeline.

| ID | Acceptance criterion |
|---|---|
| AC-6.5.1 | Incident states use status dot plus word, never colour alone. |
| AC-6.5.2 | Screen reader announces new notifications politely, not intrusively. |

### 6.6 — Guarded shipment write

Only when the capability probe says shipment writes are allowed: an operator confirms a tracking number for an order, the system re-reads the receipt, skips if the code is already there, and otherwise sends through the outbox; 403 shows "capability blocked".

| ID | Acceptance criterion |
|---|---|
| AC-6.6.1 | Without a verified capability, the write path is unreachable (UI hidden and API returns 403). |
| AC-6.6.2 | If the tracking code is already present, no request is sent to Etsy. |
| AC-6.6.3 | Every write requires an MFA session with recent re-authentication and writes an audit record. |

### 6.7 — Fulfillment automations activated

Tracking Synchronization runs on a schedule and on `order.shipped` events (idempotency key per receipt and shipment); it reads shipment records from Etsy (AutoVA writes tracking there), records tracking codes with the source of proof, and never writes a second shipment. Incident Detection & Escalation evaluates SLA rules, opens exactly one incident per root cause and resource, notifies once, and escalates by severity and age. Both specifications are completed to all 18 sections.

| ID | Acceptance criterion |
|---|---|
| AC-6.7.1 | Both specifications have no `draft` section and pass the registry loader's readiness check. |
| AC-6.7.2 | Replaying the same shipment observation N times through Tracking Synchronization (schedule and event paths together) records one tracking event, one transition to `SHIPPED` and no outbox message to Etsy. |
| AC-6.7.3 | Replaying the same breach N times through Incident Detection & Escalation yields one incident, one in-app notification and one email (extends AC-6.3.1 and AC-6.4.1 to the automation path). |
| AC-6.7.4 | When Incident Detection & Escalation itself fails until dead-lettered, the failure reaches the owner through the error-monitoring alert rules of Phase 1 and the queue health view of slice 5.9, never silently. |
| AC-6.7.5 | An escalation step that would need a non-allowlisted remediation creates a task for a person (an approval from Phase 7) and runs nothing itself. |

## Test plan

Domain tests for SLA math and incident transitions; replay, failure and recovery tests for both automations on the Postgres engine; integration tests for inspector and notifications; email adapter contract tests; UI tests; manual verification with a synthetic order in a test tenant and with a real late order if one occurs.

## Security checklist

- [ ] Email sending only from verified domains; SPF, DKIM, DMARC published (DMARC starting at `p=none`, tightened later).
- [ ] No buyer PII in emails or notifications beyond identifiers.
- [ ] Shipment write gated by capability, role, MFA and audit.

## Documentation

`docs/features/fulfillment-monitoring/`, `docs/features/incidents/`, `docs/features/notifications/`; the two automation specifications completed in `registry/automations/`; runbook entries for incident alerts.

## Demo

Create a synthetic late order in a test shop; watch the incident and email arrive once; add tracking; watch it resolve.

## Exit gate

- [ ] All AC-6.x pass; SLA windows agreed with the owner; first real-world week reviewed for false alarms.
- [ ] Tracking Synchronization and Incident Detection & Escalation are `ready`, and the owner approved their activation.

## Rollback

Disable the inspector schedule by flag; incidents remain for review.

## Risks

| Risk | Mitigation |
|---|---|
| Too many false alarms | Start with generous windows; tune from data; snooze per order |

## Cost impact

Resend free tier while volume is low.
