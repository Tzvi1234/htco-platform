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
- [ ] 20.4a Runbook links and SLOs on the web
- [ ] 20.5 Release pipelines and SBOMs for platform and clients
- [ ] 20.6 Privacy operations: retention jobs, export and deletion requests
- [ ] 20.6a Privacy requests and retention on the web
- [ ] 20.7 Office readiness review

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
| 20.4a | Web: each monitor (10.7), automation (15.5) and alert in the Engineering Console (15.7) links to its runbook entry, and the console shows each SLO with its current value and error budget | Every alert, monitor and automation shown on the web has a working runbook link |
| 20.5 | Release pipelines and SBOMs for platform and clients | Reproducible tagged releases with notes |
| 20.6 | Privacy operations: retention jobs, export and deletion requests | Retention verified on real tables, including activity records, messages and agent memory |
| 20.6a | Web: Admin › Privacy: record an export or deletion request, run it with step-up, and see its status and evidence of completion; each retention job shows its last run and how many rows it removed | Every request and run is audited, with no personal data in the audit; a deletion needs step-up |
| 20.7 | Office readiness review | See the checks below |

### Office readiness checks (slice 20.7)

| ID | Check |
|---|---|
| AC-20.7.1 | Every active employee (Eitan, Noa, Daniel, Noam, Shon, Nika, Tom, Roni, Dana, Maya, Lia, Omer, Alex, Amir, Iris) has a fully defined specification sheet, a passing evaluation run from the last 30 days, an owner-approved autonomy level and a measured routing policy. |
| AC-20.7.2 | Every automation (all 15 in `registry/automations/` are active by now) has a fully defined specification and passing replay, failure and recovery tests on staging. |
| AC-20.7.3 | Office scenarios A to J ([TESTING_STRATEGY §10](../../engineering/TESTING_STRATEGY.md#10-office-acceptance-scenarios)) are green on staging. |
| AC-20.7.4 | The kill switch of each employee and each automation, and the global one, is exercised on staging and stops new work within 60 seconds. |
| AC-20.7.5 | Gal (Phase 21) is the only employee still `draft`; no `draft` employee or automation can be triggered in production (tested). |

## Exit gate — Milestone M7

- [ ] All slices done; office readiness checks pass; owner signs off v1.0.

## Cost impact

Staging environment (a second, smaller app and Supabase project); point-in-time recovery if D-09 chooses it ([COST_MODEL](../../cost/COST_MODEL.md)).
