# Execution Order

| | |
|---|---|
| **Status** | 1.0 (2026-10-23) |
| **Rule** | Work top to bottom. A task starts only when its dependencies are merged, or are listed as running in parallel. Each task is one pull request (or a small stack) with failing tests first ([ER](../engineering/ENGINEERING_RULES.md)) |
| **Related** | [PLAN](PLAN.md), [Phase 5](phases/phase-05-orders-ingest.md), [Phase 8A](phases/phase-08a-browser-infrastructure.md), [Phase 8B](phases/phase-08b-authenticator.md) |

## 1. Phase 5 alignment and foundation

| Rank | Task | Depends on | Acceptance criteria | Size |
|---|---|---|---|---|
| 1 | **This plan PR reviewed by the owner** (ADRs 0019–0022, Phase 5 change list) | — | The owner approves, or lists changes | S |
| 2 | **Align #53**: renumber the migration after `20261022120000` and pgTAP `08` → `09`; add `correlation_id` and `causation_id` to jobs; rebase on the plan | 1 | AC-5.0.1, AC-5.0.2 (jobs part); AC-5.1.x unchanged and green | S |
| 3 | Merge #53 (slice 5.1) | 2 | CI green, Codex review handled | — |
| 4 | **Align the outbox** (done in #62, which replaced #55): renumber (migration and pgTAP `09` → `10`); add `action_execution_id` and `correlation_id` to the outbox; rebase | 3 | AC-5.0.2 (outbox part); AC-5.2.x green | S |
| 5 | Merge #62 (slice 5.2; #55 was closed) | 4 | CI green | — |
| 6 | **Align #58**: renumber (migration and pgTAP `10` → `11`); `HandlerContext`; `ExecutionState.WAITING` with its transitions; rebase | 5 | AC-5.0.3, AC-5.0.4; AC-5.10.x green | M |
| 7 | Merge #58 (slice 5.10) | 6 | CI green | — |
| 8 | Owner decision on #54 (`agent-worker`, +$5) and merge | 3, D-22 | AC-5.1 deployment notes; budget check passes | S |
| 9 | **5.12 Action Gateway and execution contract** (`action_executions`, Pydantic models, JSON Schemas) | 7 | AC-5.12.1 to AC-5.12.5 | M |
| 10 | **5.15 Breakers, limiter, concurrency** | 9 | AC-5.15.1 to AC-5.15.4 | M |
| 11 | **5.13 Router and adapters** (API real, Internal, Browser interface and fake, Human contract) | 9, 10 | AC-5.13.1 to AC-5.13.5 | M |
| 12 | **5.3 Webhooks for all 11 events** | 5, 11 | AC-5.3.1 to AC-5.3.4 | M |
| 13 | **5.4 Receipt ingest on the #57 store** | 11 | AC-5.4.1 to AC-5.4.6 | M |
| 14 | **5.5 Reconciliation and backfill on the queue** (move #57's trigger off the API process) | 13 | AC-5.5.1 to AC-5.5.5 | M |
| 15 | 5.14 Approval contract (parallel with 12–14) | 9 | AC-5.14.1 to AC-5.14.3 | S |
| 16 | 5.16 Shop Environment Registry, contracts (parallel) | 9 | AC-5.16.1 to AC-5.16.4 | S |
| 17 | 5.17 Secret Broker and Authenticator contracts (parallel) | 9 | AC-5.17.1 to AC-5.17.3 | S |
| 18 | 5.18 Status management and the chain view | 9, 13 | AC-5.18.1, AC-5.18.2 | S |
| 19 | 5.9 Health and Prometheus metrics | 7, 10 | AC-5.9.1 to AC-5.9.4 | S |
| 20 | 5.6 Orders API → 5.7 Orders UI | 13 | AC-5.6.x, AC-5.7.x | M |
| 21 | 5.11 Order automations activated (owner approval) | 12–14, 19 | AC-5.11.1 to AC-5.11.7, Scenario G and Scenario B part 1 | M |
| 22 | 5.8 Drills, then the Phase 5 exit gate | 21 | AC-5.8.1 | S |

## 2. After Phase 5

| Rank | Task | Depends on | Key acceptance | Can run in parallel with |
|---|---|---|---|---|
| 23 | Phase 6 (tracking, incidents, ship-by and low-review alerts, owner-triggered shipment writes 6.8) | 22 | AC-6.x, AC-6.8.x, AC-6.9.x | — |
| 23a | **Passkeys as a second factor (#113, owner 2026-10-25):** WebAuthn MFA through Supabase Auth next to TOTP, behind `HTCO_PASSKEYS_ENABLED`; the platform part, then Settings › Security and the sign-in step on the web | 6.10; the owner's WebAuthn MFA add-on decision (production only) | #113's ACs | 24 |
| 24 | Phase 7 (tasks, approvals on Postgres 7.11, activity, the owner's first writes 7.12) | 23 | AC-7.x, M2 gate | — |
| 24a | **AutoVA quotes (Phase 7, slices 7.13–7.17):** supplier and quote model, quote mailbox intake, deterministic parser, "New product quotes" screen, the owner's choice, and the hand-off to AutoVA as a Human task until A-02 exists | 7.11 (approvals), D-23 (mailbox), A-09 (sender and sample mails) | AC-7.13.x to AC-7.17.x | 24 |
| 25 | Phase 8 (registry tables, LangGraph checkpointer, tool bridge on the gateway 8.10) | 24 | AC-8.x | 26, 27 |
| 26 | **Pulled forward (owner, 2026-10-23): starts right after Phase 6, in parallel with Phase 7.** Phase 8A slices 8A.1–8A.5, 8A.7–8A.10: browser worker, controller, preflight, challenges, then **AutoVA recipes first** (A-01 with Export first; then A-02 approving the chosen quote), then B-02 and B-03, guards, monitoring | 24, 24a (for A-02), A-08 (AutoVA's terms), owner fills B-01–B-03 (D-16) | AC-8A.x except 8A.6; AC-8A.10.x | 25, 27 |
| 27 | Phase 8B (authenticator) | 24 | AC-8B.x | 25, 26 |
| 28 | Phase 9 (router with routed and pinned modes and automatic difficulty, ADR-0023; budgets, Jev, simple vs complex routing 9.9; the knowledge base and retrieval 9.10, ADR-0024, ready before Dana in Phase 14; Eitan) | 25 | AC-9.x | — |
| 29 | Phase 8A slice 8A.6 (Browser Use for employees) | 26, 28 | AC-8A.6.x | 30 |
| 30 | Phase 10 (supervision with the execution-chain monitors) | 28 | AC-10.x | 29 |
| 31 | Phases 11 → 12 → 13; Phase 14 in parallel with 11–13 (needs 8A and 8B for delivery A) | 30 | Phase gates | — |

## 3. Decisions that block tasks

| Decision | Blocks rank | Options (recommended first) |
|---|---|---|
| Owner review of this plan | 2–22 | Approve; or approve with changes |
| D-22 `agent-worker` cost | 8, 21 | Decided 2026-10-23: approved (ASSUMPTIONS D-22) |
| D-19 order store naming | 13 | Keep `etsy_orders`; or rename now |
| D-16 Etsy written confirmation | 26 (writes: 31) | Get it signed before write rows; verbal for B-02 and B-03 only; or no browser on Etsy |
| D-17 one egress IP disclosed to Etsy | 26 | Ask in the confirmation; or do not ask |
| D-18 egress option | 26 | Decided 2026-10-23: the same egress as the API for now (ASSUMPTIONS D-18) |
| D-23 quote mailbox | 24a | Forwarding rule to `quotes@office.ht-co.app` (Resend inbound); or read-only Gmail OAuth filtered to AutoVA's sender |
| A-08 AutoVA's terms for browser use | 26 (A-01, A-02) | The owner checks the terms; until then, the Human-task path of 6.10 and 7.17 |
| D-21 order of the first writes | 23, 24 | Shipments in Phase 6 (6.8), then shop writes (7.12); or reversible shop writes moved into Phase 6 when A-04 blocks shipments |
