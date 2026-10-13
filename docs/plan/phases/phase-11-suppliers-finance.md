# Phase 11 — Suppliers and finance

| | |
|---|---|
| **Milestone** | **M4 — Commerce core** |
| **Repositories** | both |
| **Size** | L |
| **Depends on** | Phase 10; AutoVA answers (A-06) |
| **Requirements** | FR-AGT-06, FR-FIN-01, FR-FIN-02, FR-FIN-03, FR-FIN-04, FR-MDL-08, FR-ORD-05, FR-SUP-01, FR-SUP-02, FR-SUP-03, FR-SUP-04 |
| **Status** | Not started — acceptance criteria refined at kickoff |

## Progress

- [ ] 11.1 Quote requests, quotes, mappings; manual AutoVA adapter UI
- [ ] 11.2 `supplier_quote` graph with multi-day interrupt
- [ ] 11.3 Hard rules (variant match, stock, price and shipping limits) in code
- [ ] 11.4 Optional ranking via the decision port after a benchmark of 200+ labeled cases
- [ ] 11.5 Finance ledger from Etsy payments and ledger entries (needs `billing_r` through re-consent); refunds; payouts; FX source
- [ ] 11.6 P&L views with ESTIMATED/VERIFIED labels; unit costs
- [ ] 11.7 Finance Controller agent (L0): monthly narrative and anomalies

## Goal

Supplier quotes are collected and approved once per product or variant, and become active supplier mappings that AutoVA uses for future orders. Finance shows profit per shop, product and order, with every figure marked estimated or verified.

## Scope

**In:** quote requests and the `supplier_quote` graph (interrupt while `WAITING_FOR_QUOTES`, resume by event or delayed job), hard-rule filtering in code, optional ranking after benchmarking, approval by risk, supplier mappings, re-sourcing triggers; `AutoVaPort` adapters (manual first; API if AutoVA provides one); finance ledger from Etsy payment and ledger endpoints, refunds, payouts, cost snapshots, AI and infrastructure costs; P&L views; unit-cost reporting; CSV export with audit; Finance Controller agent (explains, never records).

**Out:** paying suppliers (AutoVA's job).

## Slices (outline)

| Slice | Content | Key acceptance |
|---|---|---|
| 11.1 | Quote requests, quotes, mappings; manual AutoVA adapter UI | A mapping is created once per variant; changes open re-sourcing tasks |
| 11.2 | `supplier_quote` graph with multi-day interrupt | Waits without model calls; resumes on quote arrival or timeout |
| 11.3 | Hard rules (variant match, stock, price and shipping limits) in code | Invalid quotes never reach ranking (property tests) |
| 11.4 | Optional ranking via the decision port after a benchmark of 200+ labeled cases | False-approve rate under threshold, else rules only |
| 11.5 | Finance ledger from Etsy payments and ledger entries (needs `billing_r` through re-consent); refunds; payouts; FX source | Ledger reconciles with Etsy payouts for a full month |
| 11.6 | P&L views with ESTIMATED/VERIFIED labels; unit costs | Every figure links to its source entries |
| 11.7 | Finance Controller agent (L0): monthly narrative and anomalies | No figure in the narrative differs from the ledger |

## Security checklist

- [ ] Supplier mapping activation is high-risk (redirects purchases): owner approval with re-authentication.
- [ ] Exports audited; finance views limited to permitted roles.

## Exit gate

- [ ] A real product onboarded with an approved supplier mapping; one month of P&L reconciled with Etsy payouts.
