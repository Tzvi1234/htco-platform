# Phase 20 — Hardening and release

| | |
|---|---|
| **Milestone** | **M7 Release** |
| **Repositories** | both |
| **Size** | M |
| **Depends on** | [Phase 19](phase-19-engineering.md), [Phase 17](phase-17-android.md) and [Phase 18](phase-18-windows.md) |
| **Requirements** | NFR-SEC-01..04, NFR-PRV-01, NFR-REL-01..03, NFR-PERF-01..02, NFR-OBS-01, NFR-COST-01; FR-AGT-09 |
| **Mandatory tests owned** | **Test 13** (restore drill on the full system, RTO and RPO documented; the first drill was [Phase 4](phase-04-durable-core-order-intake.md)); **Test 15** for every worker after a rollout; re-runs tests 3, 9 and 14 at production scale ([PLAN §5](../PLAN.md#5-mandatory-tests-of-the-master-plan)) |
| **Status** | PLANNED |

Rolling wave: this phase is planned at the level of scope, slices and gates; acceptance criteria are refined at kickoff. The criteria below are the minimum.

## Progress

- [ ] 20.1 Staging environment and end-to-end suite
- [ ] 20.2 Monitoring, SLOs, alerts and runbooks
- [ ] 20.3 Backup, restore drill, RTO and RPO
- [ ] 20.4 Disaster recovery and credential rotation
- [ ] 20.5 Worker replacement keeps no state
- [ ] 20.6 Load and capacity
- [ ] 20.7 Cost baseline and budget controls
- [ ] 20.8 Email in production
- [ ] 20.9 Security review
- [ ] 20.10 Release pipelines, SBOMs and dependency upgrades
- [ ] 20.11 Privacy operations
- [ ] 20.12 Production readiness review

## Goal

Make HTCO OS dependable for the long run, then call it production ready. The master plan's milestone M7 lists what that means: **monitoring, backup restore, cost, load, email and DR**, proven by a **staging end-to-end run, the owner's approval and a production-readiness review**. In practice: a staging environment that every release passes; monitoring with service levels and a runbook for every alert; a restore drill with documented RTO and RPO (mandatory test 13); a disaster-recovery exercise; proof that a replaced worker keeps no local state (mandatory test 15); load tests including the Claude agent worker; a cost baseline with working budget stops; production email with authenticated domains; an external-quality security review; signed, reproducible releases for the platform and the three clients; privacy operations; and a readiness review of the whole office.

## Why now

Every capability exists by now: commerce (Phases 9–13), governance (14), mail (15), the three clients (16–18) and engineering (19). Hardening them together, on a staging copy of the real system, is the last step before the owner relies on HTCO for the business.

## Scope

**In:** `htco-staging` (a DigitalOcean app from the app spec in `infra/digitalocean` and a second Supabase project) with seeded synthetic data and recorded provider fixtures; an end-to-end suite on staging; monitoring (structured logs, traces with correlation IDs, worker heartbeats, queue age, stale tasks, cost, provider and API errors, SLA, incident escalation), SLOs and on-call routine; backup, restore drill, RTO and RPO; the point-in-time recovery decision (D-09); disaster recovery; credential rotation for every provider key (Anthropic, OpenAI, Jev if used, Etsy, email provider, GitHub App, Supabase, DigitalOcean); worker replacement tests; load tests of hot endpoints, the queue, the automation engine and the agent worker; cost baseline and budget controls; email domain authentication and deliverability; ASVS 5.0 Level 2 review, OWASP Agentic review per active employee, MASVS review of the native apps, penetration-style test of tenant isolation, dependency and licence audit; release automation (release-please, tagged builds, SBOMs) for the platform and the clients; privacy operations; incident response plan; EU Cyber Resilience Act applicability check for the installed clients (legal advice); the production readiness review.

**Out:** new features; new employees (Gal is [Phase 21](phase-21-growth-scale.md)); multi-region active-active hosting.

## Owner prerequisites

- D-09 decided: point-in-time recovery or daily backups, with the RTO and RPO the business accepts.
- Budget for the staging environment and, if chosen, point-in-time recovery ([COST_MODEL](../../cost/COST_MODEL.md)).
- On-call expectations: who is alerted, when, and through which channel.
- Legal advice on the Cyber Resilience Act question and on the privacy notices.
- Time for the restore drill, the DR exercise and the readiness review (each needs the owner present).

## Instructions for the agent

1. Load the skills `software-engineering`, `tdd`, `testing`, `feature-verification` and `feature-docs`; for client release work also `design-system`.
2. Read this file, then [PLAN.md](../PLAN.md) (§5 tests 13 and 15, §6 gates), [SOFTWARE_DESIGN](../../architecture/SOFTWARE_DESIGN.md) (deployment, backups, migrations), [THREAT_MODEL](../../architecture/THREAT_MODEL.md), [ADR-0016](../../adr/0016-claude-native-agent-runtime.md), [ADR-0017](../../adr/0017-auth-and-billing-policy.md), [COST_MODEL](../../cost/COST_MODEL.md), [TESTING_STRATEGY](../../engineering/TESTING_STRATEGY.md) and every feature's `operations.md`.
3. Verify at kickoff, against current official documentation and the owner's plans: Supabase backup and point-in-time recovery options and prices, DigitalOcean App Platform deployment, rollback and region behaviour, the email provider's authentication requirements, and current provider rate limits. Do not invent sizes or prices.
4. Order: 20.1 first (everything else runs on staging), then 20.2, then 20.3–20.5 (recovery), 20.6–20.8, 20.9–20.11, and 20.12 last.
5. Run destructive drills (restore, DR, worker kills, load) only on staging or on a restored copy, never on production, unless the owner schedules a production drill in writing.
6. Never copy real buyer data to staging; seed synthetic data and recorded fixtures only.
7. Never turn on `APPROVED_SUBSCRIPTION_RUNTIME` or any billing mode other than `API_PRODUCTION` for hosted runs.
8. Stop and ask the owner when: a measured RTO or RPO misses the target; a high or critical security finding appears; a cost exceeds the approved budget; a legal question has no answer; any step would touch production data.

## Slices

### 20.1 — Staging environment and end-to-end suite

| ID | Acceptance criterion |
|---|---|
| AC-20.1.1 | Staging is created from the same app spec and migrations as production, with its own secrets, keys and budgets. |
| AC-20.1.2 | Every release passes the staging end-to-end suite (order intake from signed fixtures to fulfillment, approvals, a pilot task across employees, catalog draft, ledger, mail draft, engineering request) before production. |
| AC-20.1.3 | No real buyer data exists in staging (CI scan of seeds and fixtures). |

### 20.2 — Monitoring, SLOs, alerts and runbooks

| ID | Acceptance criterion |
|---|---|
| AC-20.2.1 | Dashboards show queue age, stale tasks, worker heartbeats (agent worker, engineering runner, other workers), error rates, provider errors and 429s, budget use and SLA breaches (NFR-OBS-01). |
| AC-20.2.2 | Every alert, monitor and automation links to a runbook entry in `docs/operations/RUNBOOK.md`; SLOs are in `docs/operations/SLO.md`. |
| AC-20.2.3 | A test alert reaches the on-call channel within the agreed time, and the incident escalation automation opens one incident for it. |

### 20.3 — Backup, restore drill, RTO and RPO

Database backups (and point-in-time recovery if D-09 chooses it), a separate backup of Supabase Storage objects (not part of database backups), and a restore drill to a new project.

| ID | Acceptance criterion |
|---|---|
| AC-20.3.1 | **Mandatory test 13.** A restore drill on staging (and on a copy of production data, if the owner approves) restores the database and Storage objects; the measured RTO and RPO are within the agreed targets and recorded in `docs/operations/SLO.md` with the date. Phase 4 ran the first drill before real data; this one runs on the full system. |
| AC-20.3.2 | After the restore, open tasks, agent steps and automation executions resume or are recovered by the dispatcher and Noa's nightly supervision, with no duplicate side effect (orders, tracking pushes, emails, ledger entries). |
| AC-20.3.3 | The drill is scheduled quarterly (NFR-REL-03), with a reminder and a runbook. |

### 20.4 — Disaster recovery and credential rotation

| ID | Acceptance criterion |
|---|---|
| AC-20.4.1 | A DR exercise rebuilds the application from the app spec and the latest backup in a new app (and, if needed, a new region), re-provisions secrets from the runbook, and serves traffic; the time is recorded. |
| AC-20.4.2 | Every provider key can be rotated by the runbook without downtime; the Anthropic key rotation is exercised on staging and the AuthAndBillingPolicy keeps refusing any non-API mode ([ADR-0017](../../adr/0017-auth-and-billing-policy.md)). |
| AC-20.4.3 | The global kill switch and each billing-mode switch stop new agent runs within 60 seconds on staging, while automations keep running. |

### 20.5 — Worker replacement keeps no state

| ID | Acceptance criterion |
|---|---|
| AC-20.5.1 | **Mandatory test 15.** During a rollout on staging, every worker container (agent worker, engineering runner, browser worker and the automation worker if split) is replaced while steps are running; after the rollout no work is lost or duplicated, and everything needed to continue (tasks, state summaries, agent runs, messages, approvals, engineering requests) is in Supabase. Phase 6 proved this for the agent worker; this run covers every worker after all phases. |
| AC-20.5.2 | A scan of the worker images and runtime configuration finds no persistent volume and no business state written to local disk outside per-step temporary directories that are deleted. |

### 20.6 — Load and capacity

| ID | Acceptance criterion |
|---|---|
| AC-20.6.1 | Read endpoints meet NFR-PERF-01 (p95 under 300 ms at pilot scale) under the agreed load. |
| AC-20.6.2 | A burst of order webhooks is enqueued within 10 s at p95 with zero duplicate orders (NFR-REL-02). |
| AC-20.6.3 | The number of parallel Claude sessions per agent worker is set from measured memory and CPU per session, account rate limits and budget; a 429 storm moves tasks to waiting and never stops order intake (mandatory test 3, re-run at scale). |

### 20.7 — Cost baseline and budget controls

| ID | Acceptance criterion |
|---|---|
| AC-20.7.1 | Actual costs per component, per provider and per employee for a full month are recorded in [COST_MODEL](../../cost/COST_MODEL.md) and compared with the envelope (NFR-COST-01). |
| AC-20.7.2 | Budget thresholds alert the owner, and exhausting a budget moves tasks to waiting, with automations unaffected. |
| AC-20.7.3 | The AI cost screens show API cost and credits separately and show no invented cost for subscription usage. |

### 20.8 — Email in production

| ID | Acceptance criterion |
|---|---|
| AC-20.8.1 | `support@`, `info@` and `noreply@office.ht-co.app` pass SPF, DKIM and DMARC checks; MX and the office hostname (`app.ht-co.app/office`) do not conflict. |
| AC-20.8.2 | Bounces, complaints and inbound webhook failures are handled, monitored and linked to runbooks; no email is sent twice after a retry. |
| AC-20.8.3 | Prompt-injection test mails in the production-like staging inbox call no protected tool (mandatory test 14, re-run). |

### 20.9 — Security review

| ID | Acceptance criterion |
|---|---|
| AC-20.9.1 | ASVS 5.0 Level 2 evidence per requirement for the API and web; MASVS-aligned review of the Android and Windows apps. |
| AC-20.9.2 | OWASP Agentic review per active employee and for the engineering runner; mandatory test 9 and Scenario I re-run. |
| AC-20.9.3 | Penetration-style test of tenant and shop isolation through the API, realtime channels and every client. |
| AC-20.9.4 | No open high or critical finding; dependency and licence audit clean or accepted by the owner. |

### 20.10 — Release pipelines, SBOMs and dependency upgrades

| ID | Acceptance criterion |
|---|---|
| AC-20.10.1 | Tagged, reproducible releases with notes and an SBOM for the platform, web, Android and Windows. |
| AC-20.10.2 | Upgrading the pinned `claude-agent-sdk` (and LangGraph) runs the Phase 6 proof-of-concept gates and the options CI test before release. |
| AC-20.10.3 | Migrations are compatible with the previous release; a platform rollback restores code and configuration without a database change. |

### 20.11 — Privacy operations

| ID | Acceptance criterion |
|---|---|
| AC-20.11.1 | Retention jobs verified on real tables, including activity records, employee messages, agent runs, tool invocations, mail and agent memory. |
| AC-20.11.2 | Data subject export and deletion requests work end to end and are audited (NFR-PRV-01). |

### 20.12 — Production readiness review

| ID | Check |
|---|---|
| AC-20.12.1 | Every active employee (Maya, Shon, Noa, Omer, Eitan, Daniel, Noam, Nika, Tom, Roni, Dana, Lia, Alex, Amir, Iris) has a fully defined specification sheet, a passing evaluation run from the last 30 days, an owner-approved autonomy level, a measured routing policy and `billing_mode = API_PRODUCTION` on every hosted run. |
| AC-20.12.2 | Every automation (the 15 in `registry/automations/` plus `autova-fulfillment-watch`, `product-metrics-rollup` and `agent-mail-alias-routing`) has a fully defined specification and passing replay, failure and recovery tests on staging. |
| AC-20.12.3 | Office scenarios A to J ([TESTING_STRATEGY §10](../../engineering/TESTING_STRATEGY.md#10-office-acceptance-scenarios)) and all fifteen mandatory tests of [PLAN §5](../PLAN.md#5-mandatory-tests-of-the-master-plan) are green on staging. |
| AC-20.12.4 | The kill switch of each employee, each automation and each billing mode, and the global one, is exercised on staging and stops new work within 60 seconds. |
| AC-20.12.5 | Gal ([Phase 21](phase-21-growth-scale.md)) is the only employee still `draft`; no `draft` employee or automation can be triggered in production (tested). |

## Test plan

Staging end-to-end suite on every release; restore and DR drills with timings; worker-kill and rollout tests for every worker; load tests (API, webhooks, queue, automation engine, agent worker with fixture and real models within budget); budget exhaustion and 429 tests; email authentication checks; security review with evidence; re-run of all fifteen mandatory tests and the ten office scenarios on staging.

**Mandatory tests (PLAN §5) owned here:** 13 (database backup restore drill with RTO and RPO documented) and 15 (a replaced worker keeps no local state; everything is in Supabase after a rollout). Tests 3, 9 and 14 are re-run at production scale.

## Security checklist

- [ ] ASVS L2, MASVS and OWASP Agentic reviews done; no open high or critical finding.
- [ ] Every provider key rotated by the runbook at least once; keys per environment; no key in a client, log or prompt.
- [ ] Staging holds no real buyer data.
- [ ] Backups encrypted and restorable; access to backups limited to the owner.
- [ ] Incident response plan written and walked through with the owner.
- [ ] Threat model reviewed end to end.

## Documentation

`docs/operations/RUNBOOK.md`, `docs/operations/SLO.md` (RTO, RPO, drill results), incident response plan, DR runbook, key-rotation runbook, release runbooks for the four deliverables, SECURITY review evidence, COST_MODEL actuals, ASSUMPTIONS (D-09 closed), THREAT_MODEL, README and CLAUDE.md of both repositories, PLAN status for M7.

## Demo

Show the staging end-to-end run passing. Restore staging from backup and show the measured RTO and RPO. Replace every worker during running tasks and show nothing lost or duplicated. Stop all agent runs with the kill switch while an order still arrives and is processed. Show the monitoring dashboards and a test alert reaching the owner. Show the readiness checklist with evidence links.

## Exit gate — Milestone M7

- [ ] All slices done; mandatory tests 13 and 15 pass; all fifteen mandatory tests and scenarios A to J green on staging.
- [ ] Production readiness review passed; no open high or critical finding.
- [ ] The owner approved production.

## Rollback

Every release rolls back by code and configuration (migrations stay compatible); feature flags and kill switches stop risky features; restores follow the drilled runbook.

## Risks

| Risk | Mitigation |
|---|---|
| Daily backups give an RPO the business cannot accept | D-09 decided with measured numbers; point-in-time recovery if needed |
| Storage objects not restored with the database | Separate Storage backup and restore in the drill |
| Load tests spend real model money | Fixture models for volume; real models only in a small, budgeted run |
| Staging drifts from production | Same app spec and migrations; drift check in CI |

## Cost impact

Staging environment (a second Supabase project and a small app, about +$15 to +$25 a month in [COST_MODEL](../../cost/COST_MODEL.md)); point-in-time recovery if D-09 chooses it (from about $100 a month; verify); a small budget for load tests with real models. Prices verified at kickoff.
