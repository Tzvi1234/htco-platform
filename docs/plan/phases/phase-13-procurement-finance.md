# Phase 13 — Procurement and finance

| | |
|---|---|
| **Milestone** | **M4 — Commerce** |
| **Repositories** | both |
| **Size** | L |
| **Depends on** | [Phase 12](phase-12-product-studio.md); AutoVA answers (A-06, A-07) |
| **Requirements** | FR-AGT-09, FR-AUT-05, FR-FIN-01, FR-FIN-02, FR-FIN-03, FR-FIN-04, FR-MDL-08, FR-OPT-01, FR-ORD-05, FR-ORD-09, FR-QOS-04, FR-SUP-01, FR-SUP-02, FR-SUP-03, FR-SUP-04 |
| **Mandatory tests owned** | None of the fifteen ([PLAN §5](../PLAN.md#5-mandatory-tests-of-the-master-plan)); Tom's multi-day wait re-uses the dispatcher and quota patterns of tests 2 and 3 |
| **Status** | Not started |

Rolling wave: this phase is planned at the level of scope, slices and gates; acceptance criteria are refined at kickoff. The criteria below are the minimum.

## Progress

- [ ] 13.1 Quote requests, quotes and mappings; manual AutoVA adapter
- [ ] 13.2 Supplier Quote Request & Follow-up automation
- [ ] 13.3 Hard rules in code
- [ ] 13.4 Tom's evaluation and optional ranking
- [ ] 13.5 Inventory & Supplier Price Monitoring automation
- [ ] 13.6 Finance Ledger Ingestion automation
- [ ] 13.7 Order & Financial Reconciliation automation (daily)
- [ ] 13.8 P&L views and unit costs
- [ ] 13.9 Roni's review and narrative
- [ ] 13.10 Tom and Roni activated

## Goal

Supplier quotes are collected and approved once per product or variant, and become active supplier mappings that AutoVA uses for future orders; no supplier is re-chosen per order. Finance shows profit per shop, product and order, with every figure marked estimated or verified, and a daily reconciliation proves that Etsy, AutoVA, the CRM and the ledger agree. **Tom** (Procurement & Supplier Quotes) evaluates quotes and recommends mappings; **Roni** (Finance Controller) reviews revenue, costs and AI spend and explains them. Recording, waiting, monitoring and reconciling are deterministic automations: Supplier Quote Request & Follow-up, Inventory & Supplier Price Monitoring, Finance Ledger Ingestion and Order & Financial Reconciliation. Product Sales Counters is already active from [Phase 4](phase-04-durable-core-order-intake.md). This phase completes milestone **M4 Commerce**.

## Why now

The order, fulfillment and catalog flows are live (Phases 9–12), so there is real money to account for and real products that need suppliers. Procurement waits for days on suppliers, which the Claude-native design handles as task state with no open session; finance needs the cost ledger of [Phase 8](phase-08-models-tools.md) for AI spend and the real Etsy data of [Phase 9](phase-09-etsy-connection.md).

## Scope

**In:** quote requests, quotes and mappings; `AutoVaPort` adapters (manual first; API if AutoVA provides one, [ADR-0009](../../adr/0009-autova-integration.md)); the Supplier Quote Request & Follow-up automation (records the request, waits without model calls, follows up on a schedule, collects quotes, applies hard rules, notifies Tom); hard-rule filtering in code; Tom's evaluation of valid quotes, with optional ranking through the Decision Advisor only after a benchmark on 200+ labelled cases; approval by risk; supplier mappings; Inventory & Supplier Price Monitoring opening re-sourcing tasks; Finance Ledger Ingestion from Etsy payment and ledger endpoints, refunds, payouts, cost snapshots, AI costs (from `model_usage`) and infrastructure costs; Order & Financial Reconciliation, daily per shop, across Etsy, AutoVA, the CRM and the ledger; P&L views; unit costs per listing, ticket, inspected order, repair, shop, employee and department; CSV export with audit; Roni's monthly narrative and anomaly review (explains, never records); Supplier Quotes, Pricing & Profitability and Finance & P&L screens (clients specification screens 12, 13 and 33, first version).

**Out:** paying suppliers (AutoVA's job); Noa's nightly supervision over reconciliation results ([Phase 14](phase-14-governance.md); this phase sends findings to her pilot-scope queue); optimization proposals from the cost data (Omer's full pipeline, Phase 14).

## Owner prerequisites

- AutoVA answers (A-06, A-07): integration options, data available, whether automatic supplier ordering is active.
- Re-consent for `billing_r` if the ledger endpoints need it ([ADR-0008](../../adr/0008-etsy-integration.md)).
- Procurement policy: price and shipping limits, approval thresholds for mappings.
- Exchange-rate source and payout-lag assumptions for the P&L.
- Labelled cases for Tom's evaluation (and for the ranking benchmark if ranking is wanted) and for Roni's narrative checks.
- Approval of Tom's (proposed L2) and Roni's (proposed L0) autonomy levels and of the four automations.

## Instructions for the agent

1. Load the skills `software-engineering`, `tdd`, `testing`, `feature-verification` and `feature-docs`. Load `design-system` before any work in `htco-clients` (slices 13.1 and 13.8).
2. Read this file, then [PLAN.md](../PLAN.md), [ADR-0009](../../adr/0009-autova-integration.md), [ADR-0013](../../adr/0013-automation-engine.md), [ADR-0015](../../adr/0015-decision-advisor-and-routing.md), [ADR-0016](../../adr/0016-claude-native-agent-runtime.md), [ADR-0017](../../adr/0017-auth-and-billing-policy.md), the Phase 4, 5 and 8 files, `registry/employees/tom.toml` and `roni.toml`, the four automation documents in `registry/automations/`, `registry/tools/finance_pricing.toml` and `supplier_research.toml`, `docs/architecture/DATA_MODEL.md` (financial entries, ledger, reconciliation runs) and ENGINEERING_RULES on money handling.
3. Refresh the facts at kickoff: Etsy payment, ledger and payout endpoints and scopes; AutoVA's current answers; the exchange-rate source. Never invent fee rates or prices.
4. Work in this order: 13.1 → 13.3 → 13.2 → 13.4 → 13.5 → 13.6 → 13.7 → 13.8 → 13.9 → 13.10.
5. Money is `Decimal` with currency everywhere; ledger entries are append-only and corrected by reversing entries; only Finance Ledger Ingestion writes the ledger. Tom and Roni run as Claude-runtime steps from task briefs, with only their granted HTCO tools. Waiting for supplier quotes is task state (`WAITING_EXTERNAL`, reason quotes, the `WAITING_FOR_QUOTES` of FR-SUP-01), owned by the automation; no session stays open and no model is called while waiting.
6. Never: give Tom or Roni a tool that pays, orders, records ledger entries or activates a mapping without approval; re-select a supplier per order; do money arithmetic in a model; show an estimated figure as verified; report a failed reconciliation run as clean.
7. Stop and ask the owner when AutoVA's answers change the adapter plan, when a reconciliation month does not close, when `billing_r` is refused, or when Tom's false-approve rate misses its threshold.

## Employees and automations activated

| Kind | Name | Registry |
|---|---|---|
| Employee | Tom, Procurement & Supplier Quotes | [`tom.toml`](../../../registry/employees/tom.toml) |
| Employee | Roni, Finance Controller | [`roni.toml`](../../../registry/employees/roni.toml) |
| Automation | Supplier Quote Request & Follow-up | [`supplier-quote-followup.toml`](../../../registry/automations/supplier-quote-followup.toml) |
| Automation | Inventory & Supplier Price Monitoring | [`inventory-price-monitoring.toml`](../../../registry/automations/inventory-price-monitoring.toml) |
| Automation | Finance Ledger Ingestion | [`finance-ledger-ingestion.toml`](../../../registry/automations/finance-ledger-ingestion.toml) |
| Automation | Order & Financial Reconciliation | [`order-financial-reconciliation.toml`](../../../registry/automations/order-financial-reconciliation.toml) |

Employees follow the employee activation rule of [PLAN §6](../PLAN.md#6-gates-that-apply-to-every-phase) gate 7 as applied in [Phase 10](phase-10-fulfillment-incidents.md) slice 10.10; automations follow the rule of [Phase 4](phase-04-durable-core-order-intake.md).

## Slices

### 13.1 — Quote requests, quotes and mappings; manual AutoVA adapter

Quote requests, quotes and supplier mappings per product or variant; the manual `AutoVaPort` adapter (the owner records quotes and mapping status in HTCO with the same validation and audit as an automated adapter); the Supplier Quotes screen.

| ID | Acceptance criterion |
|---|---|
| AC-13.1.1 | A mapping is created once per variant; a change of price, stock or validity opens a re-sourcing task instead of editing the mapping in place. |
| AC-13.1.2 | Activating a mapping is high-risk: owner approval with re-authentication, audited. |

### 13.2 — Supplier Quote Request & Follow-up automation

Request, multi-day wait as task state, follow-ups by schedule, collection, hard rules (13.3), then a task for Tom with the valid quotes.

| ID | Acceptance criterion |
|---|---|
| AC-13.2.1 | The wait runs with no model call and no open runtime session; the task resumes on quote arrival or timeout through a delayed job, and Tom's next step starts from the task brief. |
| AC-13.2.2 | Replaying a trigger sends no second request to a supplier. |

### 13.3 — Hard rules in code

Variant match, stock, price and shipping limits from the procurement policy.

| ID | Acceptance criterion |
|---|---|
| AC-13.3.1 | Invalid quotes never reach Tom or ranking (property tests). |

### 13.4 — Tom's evaluation and optional ranking

Tom evaluates valid quotes (`suppliers.read_quotes`, `pricing.calculate`) and recommends a mapping through `approvals.request`; optional ranking through the Decision Advisor only after a benchmark of 200+ labelled cases.

| ID | Acceptance criterion |
|---|---|
| AC-13.4.1 | False-approve rate under the agreed threshold, else rules only; money math stays in code. |
| AC-13.4.2 | Injection text in a supplier page, quote or email changes none of Tom's tools, grants or output; Tom cannot send money or place orders (tool bridge test). |
| AC-13.4.3 | A provider 429 or budget exhaustion during Tom's step leaves the task waiting with a retry time while the quote automation keeps running. |

### 13.5 — Inventory & Supplier Price Monitoring automation

Scheduled checks of active mappings for stock, cost and margin floor.

| ID | Acceptance criterion |
|---|---|
| AC-13.5.1 | A price, stock or validity change opens one re-sourcing task for Tom (deduplicated per mapping and cause). |

### 13.6 — Finance Ledger Ingestion automation

From Etsy payments and ledger entries; refunds; payouts; FX source; supplier cost snapshots; AI cost from `model_usage`; infrastructure cost entries. The `ESTIMATED` entries created by order intake in Phase 4 become `VERIFIED` when matched.

| ID | Acceptance criterion |
|---|---|
| AC-13.6.1 | `Decimal` with currency only; a float anywhere in the money path fails a test. |
| AC-13.6.2 | The same ledger event ingested N times produces one entry; corrections are reversing entries. |
| AC-13.6.3 | AI cost entries match `model_usage` per employee, task and shop, with the billing mode recorded. |

### 13.7 — Order & Financial Reconciliation automation (daily)

Scheduled daily per shop (and on demand by a user or Roni through `automation.trigger`): compares internal orders, shipments, financial entries and the ledger with Etsy, and with AutoVA where data exists; records each comparison as a `reconciliation_runs` row with counts, differences, the external snapshot time and evidence; opens a quality finding for each difference (to Noa's queue, with Roni as owner for financial differences) and links fulfillment differences to the Phase 10 incident instead of duplicating it. Extends the Phase 4 reconciliation from "ingest what is missing" to "prove that the CRM and the ledger match".

| ID | Acceptance criterion |
|---|---|
| AC-13.7.1 | For a 200-receipt fixture with seeded differences (missing order, wrong item count, missing shipment, wrong fee, missing payout, ledger total off by one cent), each difference produces one finding with both values; a matching set produces none. |
| AC-13.7.2 | Each run records how many records it compared and the external snapshot time, so "did every Etsy order reach the CRM and the ledger" is answered from `reconciliation_runs` for any day. |
| AC-13.7.3 | The specification has all 18 sections defined; replay, failure and recovery tests pass; a failed run is retried and never reported as a clean run. |
| AC-13.7.4 | A finding closes only on evidence newer than the fix (FR-QOS-04); a recurring difference reopens it. |
| AC-13.7.5 | The ledger reconciles with Etsy payouts for one full real month. |

### 13.8 — P&L views and unit costs

P&L per shop, product, order and platform with `ESTIMATED` and `VERIFIED` labels, exchange-rate source and payout lag; unit costs per listing, ticket, inspected order, repair, shop, employee and department; Pricing & Profitability and Finance & P&L screens; CSV export.

| ID | Acceptance criterion |
|---|---|
| AC-13.8.1 | Every figure links to its source entries and shows its label and as-of time. |
| AC-13.8.2 | CSV export requires permission and writes an audit record (FR-FIN-04). |
| AC-13.8.3 | AI costs shown are API costs from the ledger; no per-token cost is invented for subscription development sessions ([ADR-0017](../../adr/0017-auth-and-billing-policy.md) rule 6). |

### 13.9 — Roni's review and narrative

Roni's monthly narrative and anomaly review from `finance.read_ledger`, `metrics.query_costs` and `activity.query`; she explains, never records.

| ID | Acceptance criterion |
|---|---|
| AC-13.9.1 | No figure in the narrative differs from the ledger (checked by code before the narrative is stored). |
| AC-13.9.2 | Roni has no tool that records or changes entries (tool bridge test). |

### 13.10 — Tom and Roni activated

Both sheets completed with `runtime = "claude"`; evaluations (Tom's quote recommendations; Roni's narratives checked against the ledger) passed; autonomy approved; routing measured.

| ID | Acceptance criterion |
|---|---|
| AC-13.10.1 | Sheets defined, evaluations passed, autonomy approved, routing measured; every run has `billing_mode = API_PRODUCTION`. |

## Test plan

Property tests for hard rules and money; replay, failure and recovery tests for the four automations (including a multi-day quote wait simulated with delayed jobs and a reconciliation run killed mid-way); the 200-receipt reconciliation fixture; the ranking benchmark if ranking is used; runtime tests with recorded model responses for Tom and Roni; evaluations; injection corpus on supplier content; a month of real ledger reconciled.

## Security checklist

- [ ] Supplier mapping activation is high-risk (redirects purchases): owner approval with re-authentication.
- [ ] Exports audited; finance views limited to permitted roles.
- [ ] Supplier pages and emails are untrusted data; Tom's tools cannot send money or place orders.
- [ ] Ledger append-only at the database level; only the ingestion automation's service identity can insert.
- [ ] No buyer PII in prompts, narratives or evaluation datasets.

## Documentation

`docs/features/suppliers/`, `docs/features/finance/`, `docs/features/reconciliation/`, `docs/features/employees/tom.md`, `docs/features/employees/roni.md`; the four automation specifications completed; ADR-0009 validation log; COST_MODEL updated with unit costs from real data.

## Demo — Milestone M4 (Commerce)

Walk one real product from its approved listing (Phase 12) to an approved supplier mapping: quote request, the multi-day wait with no session open, hard rules, Tom's recommendation, the owner's approval. Then show a real order flowing through intake, fulfillment watch and tracking, the ledger entries it produced, the daily reconciliation run with zero differences (and a seeded difference opening a finding in staging), the month's P&L with verified figures and Roni's narrative.

## Exit gate

- [ ] All AC-13.x pass.
- [ ] A real product onboarded with an approved supplier mapping; one month of P&L reconciled with Etsy payouts; daily reconciliation running in production.
- [ ] Tom, Roni and the four automations are `ready` and approved.
- [ ] M4 definition of done from the master plan: Etsy and AutoVA permissions proved by probes, duplicate and timeout cases tested, every money-moving or publishing action behind a safe approval.

## Rollback

Employee and automation kill switches; mappings can be deactivated through an approved action; ledger entries are append-only and corrected by reversing entries; reconciliation can be paused without losing data (the next run covers the gap).

## Risks

| Risk | Mitigation |
|---|---|
| AutoVA offers no API | Manual adapter first (FR-SUP-04); browser automation only through a new ADR within AutoVA's terms |
| Ledger endpoints need scopes the shop has not granted | Re-consent planned at kickoff; estimates stay labelled until verified |
| Reconciliation produces noisy findings | Tolerances agreed with the owner; deduplication per resource and cause |

## Cost impact

AI cost per quote evaluation and per monthly narrative, measured at the gate; no new infrastructure.
