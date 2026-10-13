# Phase 16 — Production hardening and v1.0 launch

| | |
|---|---|
| **Milestone** | **M7 — v1.0** |
| **Repositories** | both |
| **Size** | M |
| **Depends on** | Phases 11–15 |
| **Requirements** | Non-functional: NFR-SEC-01..04, NFR-PRV-01, NFR-REL-01..03, NFR-PERF-01..02, NFR-OBS-01 |
| **Status** | Not started — acceptance criteria refined at kickoff |

## Progress

- [ ] 16.1 Staging environment and promotion flow
- [ ] 16.2 Recovery: objectives, drill, documented results
- [ ] 16.3 Security review: ASVS L2 evidence, agentic controls, penetration-style test of tenant isolation
- [ ] 16.4 Runbooks, SLOs, alert review
- [ ] 16.5 Release pipelines and SBOMs for platform and clients
- [ ] 16.6 Privacy operations: retention jobs, export and deletion requests

## Goal

Make HTCO OS dependable for the long run: a staging environment, verified recovery, an external-quality security review, runbooks and service levels, release pipelines with SBOMs, and a cost and alerting baseline. Then call it v1.0.

## Scope

**In:** `htco-staging` app and Supabase project with seeded synthetic data; recovery objectives (RTO/RPO) and the point-in-time recovery decision (D-09); restore drill and failover exercise; ASVS 5.0 Level 2 review with evidence per requirement; OWASP Agentic review; dependency and licence audit; load test of hot endpoints and the queue; runbooks for every alert; SLOs and on-call routine; release automation (release-please, tagged builds, SBOM attached); privacy review (retention jobs, data subject requests); incident response plan; EU Cyber Resilience Act applicability check for installed clients (legal advice).

## Slices (outline)

| Slice | Content | Key acceptance |
|---|---|---|
| 16.1 | Staging environment and promotion flow | Every release passes staging before production |
| 16.2 | Recovery: objectives, drill, documented results | Restore within the agreed RTO; data loss within RPO |
| 16.3 | Security review: ASVS L2 evidence, agentic controls, penetration-style test of tenant isolation | No open high or critical findings |
| 16.4 | Runbooks, SLOs, alert review | Every alert links to a runbook entry |
| 16.5 | Release pipelines and SBOMs for platform and clients | Reproducible tagged releases with notes |
| 16.6 | Privacy operations: retention jobs, export and deletion requests | Retention verified on real tables |

## Exit gate — Milestone M7

- [ ] All slices done; owner signs off v1.0.
