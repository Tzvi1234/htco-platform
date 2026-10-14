# Phase 18 — Continuous optimization

| | |
|---|---|
| **Milestone** | — |
| **Repositories** | both |
| **Size** | M |
| **Depends on** | [Phase 13](phase-13-suppliers-finance.md) (real cost data); Maya from [Phase 15](phase-15-office-executive.md) prioritizes proposals (if this phase starts before Phase 15 ends, the owner prioritizes) |
| **Requirements** | FR-AGT-09, FR-AUT-05, FR-FIN-03, FR-MDL-05, FR-OPT-01, FR-OPT-02, FR-OPT-03 |
| **Status** | Not started |

Rolling wave: this phase is planned at the level of scope, slices and gates; acceptance criteria are refined at kickoff with the cost data of Phases 9–15. The criteria below are the minimum.

## Progress

- [ ] 18.1 Unit-of-work metrics and FinOps views
- [ ] 18.2 Optimization proposals store and lifecycle
- [ ] 18.3 Optimization Review Scheduling automation
- [ ] 18.4 Omer activated
- [ ] 18.5 Measurement, verification and rollback
- [ ] 18.6 Scenario D end to end

**Foundation already in code.** `htco_domain.improvement` implements the proposal lifecycle `DETECTED → PROPOSED → EVALUATING → APPROVED / REJECTED → DEVELOPMENT → TESTING → DEPLOYED → MEASURING → VERIFIED / ROLLED_BACK` (`OptimizationProposal`, `ProposalState`; a proposal needs acceptance criteria and a rollback plan before `PROPOSED`), tested in `tests/domain/test_lifecycles.py`. The metrics, store, automation, Omer's workflow and the measurement step are planned in this phase.

## Goal

The office gets cheaper and better per unit of work, with evidence. Cost, latency, failure and quality are measured per unit of work, per employee and per department. **Omer** (Continuous Optimization Officer) finds waste (AI reasoning that could be a deterministic automation, expensive model choices, excessive tokens, repeated tool calls, duplicated workflows, inefficient handoffs) and writes structured proposals with a baseline, evidence, expected benefit, risk, rollback plan and acceptance criteria. Maya prioritizes; approved work becomes an engineering request implemented through pull requests and CI; after deployment Omer measures and recommends verify or roll back. **Omer proposes; he never implements, deploys, approves his own proposals or changes routing policies.**

## Why now

By Phase 13 the office has months of real records: `model_usage`, `routing_decisions`, `tool_invocations`, `automation_executions`, `activity_records`, `quality_findings` and the ledger. Optimizing earlier would have meant guessing.

## Scope

**In:** metrics: cost per product researched, per approved listing, per completed order, per resolved customer request, per employee and per department; token usage and provider expenses; workflow success, failure and retry rates; tool usage frequency; latency and queue wait; quality outcomes and rejection rates; repeated manual actions; repeated reasoning that could be deterministic. FinOps views. `optimization_proposals` and `optimization_measurements`; the Optimization Review Scheduling automation; Omer's activation; measurement with verify or roll back; Scenario D.

**Out:** engineering employees implementing the work (Alex and Iris from [Phase 19](phase-19-engineering.md); until then people implement it with their usual tools, through the same pull request, CI and release gates); routing experiments and model A/B tests ([Phase 21](phase-21-growth-scale.md)).

## Owner prerequisites

- Agreement on the units of work and their cost targets.
- Approval of Omer's autonomy level (proposed L0: suggests only) and of the automation.

## Employees and automations activated

| Kind | Name | Registry |
|---|---|---|
| Employee | Omer, Continuous Optimization Officer | [`omer.toml`](../../../registry/employees/omer.toml) |
| Automation | Optimization Review Scheduling | [`optimization-review-scheduling.toml`](../../../registry/automations/optimization-review-scheduling.toml) |

Omer follows the activation rule of [Phase 9](phase-09-router-advisor.md) slice 9.7; the automation the rule of [Phase 5](phase-05-orders-ingest.md).

## Slices

### 18.1 — Unit-of-work metrics and FinOps views

Deterministic metric jobs over the records above, attributing cost to units of work through correlation and task IDs; views per unit, employee, department, workflow and shop, with trends.

| ID | Acceptance criterion |
|---|---|
| AC-18.1.1 | Every metric states its formula, source tables and as-of time; cost figures reconcile with `model_usage` and the ledger for the same period. |
| AC-18.1.2 | "Repeated reasoning that could be deterministic" is detected by a deterministic rule (the same employee, task type and inputs producing the same decision repeatedly), not by a model reading every session. |

### 18.2 — Optimization proposals store and lifecycle

`optimization_proposals` (title, detecting actor, state, problem, affected employees or workflows, baseline, evidence links, expected benefit, risk, rollback plan, acceptance criteria, `deterministic_candidate`) and `optimization_measurements` (before and after, with periods and sample sizes); API and an Executive Office area (built in Phase 15) filled with real data.

| ID | Acceptance criterion |
|---|---|
| AC-18.2.1 | Every transition follows `htco_domain.improvement`; a proposal without acceptance criteria and a rollback plan cannot reach `PROPOSED`; every transition writes an activity record. |
| AC-18.2.2 | Only Maya or the owner can move a proposal to `APPROVED` or `REJECTED`; Omer cannot decide his own proposal (policy test). |

### 18.3 — Optimization Review Scheduling automation

Computes the metrics on schedule, triggers Omer's periodic analysis, and starts a threshold-based review when a metric crosses its limit (for example cost per approved listing up 30% week on week).

| ID | Acceptance criterion |
|---|---|
| AC-18.3.1 | The specification has all 18 sections defined; replay, failure and recovery tests pass; a threshold crossing triggers one review, not one per run. |

### 18.4 — Omer activated

Omer reads metrics and activity, and writes proposals through `optimization.propose`; for approved proposals he creates the engineering request through `engineering.create_request`. He has no tool that changes code, configuration, routing policies or deployments.

| ID | Acceptance criterion |
|---|---|
| AC-18.4.1 | Omer's sheet has no `draft` section; his evaluation set (cases with known waste and known non-issues) passes; the owner approved his autonomy; his routing policy is measured. |
| AC-18.4.2 | Every proposal cites a baseline from recorded data and an evidence link; a proposal with an unsupported number fails validation. |
| AC-18.4.3 | Omer's attempts to change a policy, a grant or a deployment are denied by the tool bridge and recorded. |

### 18.5 — Measurement, verification and rollback

After deployment, the proposal moves to `MEASURING`; the measurement compares the agreed metric over a comparable period and checks quality guards (rejection rates, findings, incidents). Omer recommends `VERIFIED` or `ROLLED_BACK`; the decision is Maya's or the owner's, and a rollback uses the change's own rollback plan.

| ID | Acceptance criterion |
|---|---|
| AC-18.5.1 | A proposal reaches `VERIFIED` only when its acceptance criteria are met and no quality guard regressed; otherwise it is rolled back or returned with evidence. |
| AC-18.5.2 | Measurements record their periods and sample sizes; a measurement with too small a sample is reported as inconclusive, not as a success. |

### 18.6 — Scenario D end to end

| ID | Acceptance criterion |
|---|---|
| AC-18.6.1 | **Scenario D** ([TESTING_STRATEGY §10](../../engineering/TESTING_STRATEGY.md#10-office-acceptance-scenarios)): Omer detects repeated model reasoning that a rule can replace, proposes it with a baseline; Maya prioritizes; an engineering request (a GitHub issue until Phase 19's request records) is implemented through a pull request with CI green; the change is deployed in stages behind a flag; measurement shows lower cost per unit with no quality loss, and the proposal reaches `VERIFIED`. The rollback path is exercised once on a second proposal. |

## Test plan

Metric jobs tested on fixtures with known answers; lifecycle and policy tests; automation replay and recovery tests; Omer's evaluation set; Scenario D as a staged end-to-end run on the deployed system.

## Security checklist

- [ ] Omer has read access to metrics and activity, and no write access to code, configuration, policies or deployments.
- [ ] Metrics and proposals mask buyer PII and never include model reasoning.

## Documentation

`docs/features/optimization/`, `docs/features/employees/omer.md`; COST_MODEL updated with unit costs and verified savings.

## Exit gate

- [ ] All AC-18.x pass, including Scenario D.
- [ ] At least one verified saving per unit with no quality regression.
- [ ] Omer and Optimization Review Scheduling are `ready` and approved.

## Rollback

Omer's kill switch; the automation's kill switch; every deployed proposal carries its own rollback plan.

## Risks

| Risk | Mitigation |
|---|---|
| Savings that degrade quality | Quality guards in every measurement; rollback by default when a guard regresses |
| Noisy metrics lead to false proposals | Minimum sample sizes; comparable periods; inconclusive is a valid result |

## Cost impact

Omer's model usage, measured; expected net saving from verified proposals, recorded at the gate.
