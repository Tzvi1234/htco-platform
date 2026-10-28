# Phase 20 — Production hardening and v1.0 launch

| | |
|---|---|
| **Milestone** | **M7 — v1.0** |
| **Repositories** | both |
| **Size** | M |
| **Depends on** | [Phase 19](phase-19-engineering.md), [Phase 16](phase-16-android.md) and [Phase 17](phase-17-windows.md) |
| **Requirements** | Non-functional: NFR-SEC-01..04, NFR-PRV-01, NFR-REL-01..03, NFR-PERF-01..02, NFR-OBS-01; FR-AGT-09 |
| **Status** | Not started |

Rolling wave: this phase is planned at the level of scope, slices and gates; acceptance criteria are refined at kickoff. The "key acceptance" column below is the minimum.

## Progress

- [ ] 20.1 Staging environment and promotion flow
- [ ] 20.2 Recovery: objectives, drill, documented results
- [ ] 20.3 Security review: ASVS L2 evidence, agentic controls, penetration-style test of tenant isolation
- [ ] 20.4 Runbooks, SLOs, alert review
- [ ] 20.5 Release pipelines and SBOMs for platform and clients
- [ ] 20.6 Privacy operations: retention jobs, export and deletion requests
- [ ] 20.7 Office readiness review
- [ ] 20.8 Prometheus `/metrics` for the queue, executions and sync (added 2026-10-28: AC-5.9.4, moved from 5.9)

## Architecture upgrade (2026-10-23)

Changes from [ADR-0019](../../adr/0019-unified-execution-engine-and-action-gateway.md) (Action Gateway, four channels), [ADR-0022](../../adr/0022-etsy-write-tools-and-browser-scope.md) (Etsy write tools and browser scope), [ADR-0020](../../adr/0020-per-shop-browser-environment.md) and [ADR-0021](../../adr/0021-authenticator-service.md):

- The security review covers the gateway, browser workers (profile theft, session hijack, injection), the authenticator and the secret broker.
- The recovery drill restores `totp_accounts` and sealed profiles.
- Runbooks cover a suspended environment, a challenge spike and a suspected secret exposure.

## Goal

Make HTCO OS dependable for the long run: a staging environment, verified recovery, an external-quality security review, runbooks and service levels, release pipelines with SBOMs, a cost and alerting baseline, and a readiness review of the whole office (every active employee and automation evaluated, every office scenario green, kill switches proven). Then call it v1.0.

## Scope

**In:** `htco-staging` app and Supabase project with seeded synthetic data; recovery objectives (RTO/RPO) and the point-in-time recovery decision (D-09); restore drill and failover exercise; ASVS 5.0 Level 2 review with evidence per requirement; OWASP Agentic review across all active employees; dependency and licence audit; load test of hot endpoints, the queue and the automation engine; runbooks for every alert, monitor and automation; SLOs and on-call routine; release automation (release-please, tagged builds, SBOM attached); privacy review (retention jobs, data subject requests); incident response plan; EU Cyber Resilience Act applicability check for installed clients (legal advice); the office readiness review.

## Slices (outline)

| Slice | Content | Key acceptance |
|---|---|---|
| 20.1 | Staging environment and promotion flow | Every release passes staging before production |
| 20.2 | Recovery: objectives, drill, documented results | Restore within the agreed RTO; data loss within RPO; agent sessions and automation executions resume or are recovered by the nightly supervisor after the restore |
| 20.3 | Security review: ASVS L2 evidence, agentic controls per active employee, penetration-style test of tenant isolation | No open high or critical findings |
| 20.4 | Runbooks, SLOs, alert review | Every alert, monitor and automation links to a runbook entry |
| 20.5 | Release pipelines and SBOMs for platform and clients | Reproducible tagged releases with notes |
| 20.6 | Privacy operations: retention jobs, export and deletion requests | Retention verified on real tables, including activity records, messages and agent memory |
| 20.7 | Office readiness review | See the checks below |
| 20.8 | Prometheus `/metrics` for the queue, executions and sync (AC-5.9.4, moved here from 5.9 by the audit of 2026-10-28) | See AC-20.8.1 and AC-20.8.2 below |

### Office readiness checks (slice 20.7)

| ID | Check |
|---|---|
| AC-20.7.1 | Every active employee (Eitan, Noa, Daniel, Noam, Shon, Nika, Tom, Roni, Dana, Maya, Lia, Omer, Alex, Amir, Iris) has a fully defined specification sheet, a passing evaluation run from the last 30 days, an owner-approved autonomy level and a measured routing policy. |
| AC-20.7.2 | Every automation (all 15 in `registry/automations/` are active by now) has a fully defined specification and passing replay, failure and recovery tests on staging. |
| AC-20.7.3 | Office scenarios A to J ([TESTING_STRATEGY §10](../../engineering/TESTING_STRATEGY.md#10-office-acceptance-scenarios)) are green on staging. |
| AC-20.7.4 | The kill switch of each employee and each automation, and the global one, is exercised on staging and stops new work within 60 seconds. |
| AC-20.7.5 | Gal (Phase 21) is the only employee still `draft`; no `draft` employee or automation can be triggered in production (tested). |

### Metrics export (slice 20.8)

Slice 5.9 defined the series and the owner's decision of 2026-10-23 moved AC-5.9.4 to Phase 8A's monitoring slice, but 8A.8 lists only browser series. The export lands here, with the SLOs of 20.4 that read it, unless 8A.8 picks a monitoring host first, in which case it moves forward unchanged. No web step: the same figures are on Admin › Health (5.9) and in the Engineering Console (15.7); this slice only exports them.

| ID | Acceptance criterion |
|---|---|
| AC-20.8.1 | The API and the worker expose `/metrics` (Prometheus text format) on a port or path that only the monitoring host can reach, with the series of 5.9: queue depth and age per queue, dead letters, worker heartbeat age, executions by automation and outcome with retries and latency, each shop's sync age, webhook events received and unprocessed by type, and each circuit breaker's state. |
| AC-20.8.2 | No label holds an organization name, a buyer field or a free-text error: only IDs that are not personal, and shop IDs (AC-5.9.4); a test scrapes the endpoint with seeded data and checks every label. An unauthenticated request from outside the monitoring network is refused. |

## Exit gate — Milestone M7

- [ ] All slices done; office readiness checks pass; owner signs off v1.0.

## Cost impact

Staging environment (a second, smaller app and Supabase project); point-in-time recovery if D-09 chooses it ([COST_MODEL](../../cost/COST_MODEL.md)).
