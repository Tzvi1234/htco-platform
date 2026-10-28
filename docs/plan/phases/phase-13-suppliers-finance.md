# Phase 13 — Suppliers and finance

| | |
|---|---|
| **Milestone** | **M4 — Commerce core** |
| **Repositories** | both |
| **Size** | L |
| **Depends on** | [Phase 12](phase-12-product-studio.md); AutoVA answers (A-06) |
| **Requirements** | FR-AGT-09, FR-AUT-05, FR-FIN-01, FR-FIN-02, FR-FIN-03, FR-FIN-04, FR-MDL-08, FR-OPT-01, FR-ORD-05, FR-SUP-01, FR-SUP-02, FR-SUP-03, FR-SUP-04 |
| **Status** | Not started |

Rolling wave: this phase is planned at the level of scope, slices and gates; acceptance criteria are refined at kickoff. The "key acceptance" column below is the minimum.

## Progress

- [ ] 13.1 AutoVA read side and non-AutoVA suppliers on the Phase 7 quote model
- [ ] 13.2 Supplier Quote Request & Follow-up automation
- [ ] 13.3 Hard rules in code
- [ ] 13.4 Tom's evaluation and optional ranking
- [ ] 13.5 Inventory & Supplier Price Monitoring automation
- [ ] 13.6 Finance Ledger Ingestion automation
- [ ] 13.7 P&L views and unit costs
- [ ] 13.8 Roni's review and narrative
- [ ] 13.9 Tom and Roni activated
- [ ] 13.10 Money actions that the API cannot do (Human channel; browser rows B-08 to B-11 only if scoped)

## Architecture upgrade (2026-10-23)

Changes from [ADR-0019](../../adr/0019-unified-execution-engine-and-action-gateway.md) (Action Gateway, four channels), [ADR-0022](../../adr/0022-etsy-write-tools-and-browser-scope.md) (Etsy write tools and browser scope):

- **Finance:** Finance Ledger Ingestion reads `etsy.ledger.read` and `etsy.payments.read` through the gateway.
- **Price and stock:** Inventory & Supplier Price Monitoring proposes `etsy.inventory.update` (W2) through approvals. Each shop starts owner-trigger first.
- **POD and supplier sites** without an API (tracking, order status) are read by deterministic browser recipes only with their own scope rows (Phase 8A engine), or handed to a person.
- **Money actions Etsy's API lacks** (refunds, cancellations, coupons and sales, Ads budgets) are Human-channel tasks with the approved values. Browser rows B-08 to B-11 are used only if the owner records their scope and the written confirmation exists (ADR-0022).

## AutoVA flow alignment (2026-10-23)

The owner's real flow is in the [AutoVA integration](../../architecture/autova-integration.md) page.

- **One home for suppliers and quotes.** The supplier, quote and mapping model, the quote mailbox intake, the deterministic parser, the "New product quotes" screen and the owner's choice moved to **Phase 7 (slices 7.13–7.17)**. The Supplier Quote Request & Follow-up automation is activated there. AutoVA requests the quotes itself; HTCO collects them, waits and reminds.
- **AutoVA recipes.** A-02 (approve the chosen quote in AutoVA) and A-01 with Export first are in **Phase 8A (slice 8A.10)**.
- **This phase extends the model, not a second one:**
  - 13.1 becomes the `AutoVaPort` read side over `autova_orders` (from A-01), plus manual supplier entries for non-AutoVA suppliers;
  - 13.2 adds follow-ups for non-AutoVA suppliers;
  - 13.4 adds Tom's evaluation and recommendation **on the same quote screen**. The owner still decides each quote (W3).
- **Payment stays AutoVA's Auto Pay.** HTCO never pays.
- **Profit per order:** AutoVA's profit field (`autova_orders.autova_profit`, labelled `ESTIMATED`, source `autova`) is shown at once. Finance Ledger Ingestion cross-checks it against the Etsy ledger (fees, refunds) and the supplier cost, and a difference above a threshold opens a finding. The verified figure is the ledger's.

## Goal

Supplier quotes are collected and approved once per product or variant, and become active supplier mappings that AutoVA uses for future orders. Finance shows profit per shop, product and order, with every figure marked estimated or verified. **Tom** (Procurement & Supplier Quotes) evaluates quotes and recommends mappings; **Roni** (Finance Controller) reviews revenue, costs and AI spend and explains them. Recording, waiting and monitoring are deterministic automations: Supplier Quote Request & Follow-up, Finance Ledger Ingestion, and Inventory & Supplier Price Monitoring. Product Sales Counters is already active from [Phase 5](phase-05-orders-ingest.md); Order & Financial Reconciliation from [Phase 10](phase-10-supervision.md) now covers the full ledger.

## Scope

**Shop statistics (owner, 2026-10-15).** Views, visits and conversion are not in the Etsy API. At kickoff we evaluate two sources:
- whether an Etsy App Marketplace tool the owner already uses (eRank, Alura, Marmalead…) offers an API or an export;
- the official Shop Manager CSV export, which the owner uploads and HTCO parses.

No scraping of etsy.com.


**In:** quote requests, quotes and mappings; the Supplier Quote Request & Follow-up automation (records the request, waits in `WAITING_FOR_QUOTES` without model calls, collects quotes, applies hard rules, notifies Tom); hard-rule filtering in code; Tom's evaluation of valid quotes, with optional ranking through the Decision Advisor only after a benchmark on 200+ labelled cases; approval by risk; supplier mappings; Inventory & Supplier Price Monitoring opening re-sourcing tasks; `AutoVaPort` adapters (manual first; API if AutoVA provides one); Finance Ledger Ingestion from Etsy payment and ledger endpoints, refunds, payouts, cost snapshots, AI and infrastructure costs (from `model_usage`); P&L views; unit costs per listing, ticket, inspected order, repair, shop, employee and department; CSV export with audit; Roni's monthly narrative and anomaly review (explains, never records).

**Out:** paying suppliers (AutoVA's job); optimization proposals from the cost data ([Phase 18](phase-18-optimization.md)).

**Etsy fee entries (moved from 5.4, [ASSUMPTIONS](../../product/ASSUMPTIONS.md) D-27).** Slice 5.4's text asked for estimated Etsy fee entries per order, but the receipt carries no fees, so none were built. Finance Ledger Ingestion (13.6) records them from Etsy's payment and ledger entries instead (transaction, processing, listing and Ads fees, per order where the ledger links one), and they feed the order profit and fee share of the P&L views. Until then no fee figure is shown anywhere.

## Owner prerequisites

- AutoVA answers (A-06): integration options and data available.
- Re-consent for `billing_r` if the ledger endpoints need it (ADR-0008).
- Procurement policy: price and shipping limits, approval thresholds for mappings.
- Approval of Tom's (proposed L2) and Roni's (proposed L0) autonomy levels and of the three automations.

## Employees and automations activated

| Kind | Name | Registry |
|---|---|---|
| Employee | Tom, Procurement & Supplier Quotes | [`tom.toml`](../../../registry/employees/tom.toml) |
| Employee | Roni, Finance Controller | [`roni.toml`](../../../registry/employees/roni.toml) |
| Automation | Supplier Quote Request & Follow-up | [`supplier-quote-followup.toml`](../../../registry/automations/supplier-quote-followup.toml) |
| Automation | Finance Ledger Ingestion | [`finance-ledger-ingestion.toml`](../../../registry/automations/finance-ledger-ingestion.toml) |
| Automation | Inventory & Supplier Price Monitoring | [`inventory-price-monitoring.toml`](../../../registry/automations/inventory-price-monitoring.toml) |

Employees follow the activation rule of [Phase 9](phase-09-router-advisor.md) slice 9.7; automations the rule of [Phase 5](phase-05-orders-ingest.md).

## Slices (outline)

| Slice | Content | Key acceptance |
|---|---|---|
| 13.1 | Builds on the Phase 7 model (7.13): the `AutoVaPort` read side over `autova_orders` (A-01) and manual entries for non-AutoVA suppliers | No second quote or mapping table (schema inventory test); changes open re-sourcing tasks |
| 13.2 | Supplier Quote Request & Follow-up (active since Phase 7 for AutoVA quotes) extended to non-AutoVA suppliers: requests, multi-day waits, follow-ups | Waits without model calls or open runtime sessions; replay sends no second request |
| 13.3 | Hard rules (variant match, stock, price and shipping limits) in code | Invalid quotes never reach Tom or ranking (property tests) |
| 13.4 | Tom evaluates valid quotes and recommends one on the Phase 7 quote screen; optional ranking through the Decision Advisor after a benchmark of 200+ labelled cases; the owner still decides each quote (W3) | False-approve rate under threshold, else rules only; money math stays in code |
| 13.5 | Inventory & Supplier Price Monitoring for active mappings | A price, stock or validity change opens one re-sourcing task for Tom |
| 13.6 | Finance Ledger Ingestion from Etsy payments and ledger entries; refunds; payouts; FX source; AI cost from `model_usage` | Ledger reconciles with Etsy payouts for a full month (through Order & Financial Reconciliation); Decimal only |
| 13.7 | P&L views with `ESTIMATED`/`VERIFIED` labels; unit costs per employee and department | Every figure links to its source entries |
| 13.8 | Roni's monthly narrative and anomaly review | No figure in the narrative differs from the ledger; Roni has no tool that records entries |
| 13.9 | Tom and Roni activated | Sheets defined, evaluations passed, autonomy approved, routing measured |
| 13.10 | Refunds, cancellations, coupons, sales and Ads budget changes as Human tasks with approved values; the monthly statement download (B-11) as a recipe if scoped | No money action runs without an Owner (W3) approval; a browser money action is refused while its scope row is empty or the written confirmation is missing |

## Test plan

Property tests for hard rules and money; replay, failure and recovery tests for the three automations (including a multi-day wait simulated with delayed jobs); the ranking benchmark if ranking is used; evaluations for Tom (quote recommendations) and Roni (narratives checked against the ledger); a month of real ledger reconciled.

## Security checklist

- [ ] Supplier mapping activation is high-risk (redirects purchases): owner approval with re-authentication.
- [ ] Exports audited; finance views limited to permitted roles.
- [ ] Supplier pages and emails are untrusted data; Tom's tools cannot send money or place orders.

## Documentation

`docs/features/suppliers/`, `docs/features/finance/`, `docs/features/employees/tom.md`, `docs/features/employees/roni.md`; the three automation specifications completed; COST_MODEL updated with unit costs from real data.

## Exit gate

- [ ] A real product onboarded with an approved supplier mapping; one month of P&L reconciled with Etsy payouts.
- [ ] Tom, Roni and the three automations are `ready` and approved.

## Rollback

Employee and automation kill switches; mappings can be deactivated through an approved action; ledger entries are append-only and corrected by reversing entries.

## Risks

| Risk | Mitigation |
|---|---|
| AutoVA offers no API | Manual adapter first (FR-SUP-04); browser automation only as a last resort within AutoVA's terms |
| Ledger endpoints need scopes the shop has not granted | Re-consent planned at kickoff; estimates stay labelled until verified |

## Cost impact

AI cost per quote evaluation and per monthly narrative, measured; no new infrastructure.
