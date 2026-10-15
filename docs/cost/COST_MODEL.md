# Cost Model

| | |
|---|---|
| **Prices checked** | 2026-10-14 (list prices, USD, excluding VAT). Re-check on the day any service is created. |
| **Rule** | Every phase updates the "actual" column and explains changes over 20%. |

## 1. Scenarios

| Scenario | Shops | Orders / month | New listings / month | Listing audits / month | Support threads / month |
|---|---|---|---|---|---|
| **Pilot** | 1 | about 30–60 (the pilot shop averages about one order a day) | 10 | 64 (full catalog, then changes only) | 20 |
| **Growing store** | 1–2 | 300 | 50 | 200 | 100 |
| **Multi-store** | 5 | 1,500 | 200 | 800 | 500 |

## 2. Fixed infrastructure (Pilot, from Phase 5)

This is the only place infrastructure prices are kept; other documents link here.

| Item | Lean | Recommended | Notes |
|---|---|---|---|
| Supabase Pro (Micro compute included) | 25 | 25 | 8 GB disk, 250 GB egress, 100 GB storage, 100k MAU included |
| Supabase IPv4 add-on | 4 | 4 | Required for direct connections from DigitalOcean |
| DigitalOcean `api` | 5 | 12 | 0.5 GB → 1 GB shared CPU |
| DigitalOcean `agent-worker` | 5 | 12 | From Phase 5: automations, then AI employees (no container per employee) |
| DigitalOcean `web` | 0 | 0 | Static site inside the paid app (client ADR-0006); the deploy refuses a spec over the $5 budget |
| Container registry | 0 | 5 | DigitalOcean Starter or GHCR within GitHub Pro storage |
| Resend | 0 | 20 | Free until inbound mail (Phase 14) |
| GitHub Pro | 4 | 4 | Rulesets on private repositories |
| Error monitoring | 0 | 0 | Free developer tier |
| **Total before AI** | **48** | **87** | |
| Staging (when created) | +15 | +25 | Second Supabase project (Micro, about $10) plus a small app |

Before Phase 5 the bill is smaller (no worker; Supabase from Phase 2). A separate `automation-worker` (+5 to +12) is added only when measured load or isolation justifies it. Optional later: point-in-time recovery from about $100/month (needs larger compute), dedicated egress IP up to $25, log drains $60, Windows code signing about $10/month, Google Play developer account $25 one-time.

## 3. Variable AI cost (planning method)

AI cost is computed per unit of work, not per model:

```
cost_per_unit = Σ over model calls ( input_tokens × input_price
                                     + cached_tokens × cache_price
                                     + output_tokens × output_price ) × (1 + retry_rate)
```

| Unit | Model tier (planned) | Planning range per unit | Notes |
|---|---|---|---|
| Order intake, counters, tracking sync, ledger, reconciliation | none (automations) | $0 | Deterministic workflows; version 1.0 planned agents for parts of this |
| Listing audit (Daniel) | economy, vision for photos | $0.02–0.10 | Batch API halves cost for non-urgent audits |
| New listing draft (Shon, Noam, Nika, Daniel) | economy to balanced; advanced only on escalation | $0.10–0.40 | Images extra; publishing is an automation |
| Supplier quote evaluation (Tom) | rules first; Decision Advisor; economy for ranking | under $0.01 | Most work is code |
| Exception investigation (Eitan) | economy, escalate to balanced | $0.005–0.05 | Only on exceptions |
| Supervision (Noa) | rules and monitors; economy for samples | $0.002–0.02 per reviewed item | Risk-based sampling, never one call per event |
| Structured decisions (Jev) | decision model | about $0.00002 per three-question call (vendor price, unverified) | Rules first; benchmarked before use |
| Support draft (Dana) | economy to balanced | $0.01–0.05 | |
| Engineering work (Alex, Amir, Iris) | advanced | $0.50–3.00 per merged change | Capped per task by each routing policy; Phase 19 |

Prices per model live in the model catalog with an as-of date (Phase 9), not in this document. Every employee's routing policy carries a per-task cap (see `registry/employees/*.toml`), so the worst case per unit is bounded. Pilot AI spend is expected to stay under the cap the owner sets (decision D-06).

## 4. Unit economics to report (from Phase 13; analysed by Omer from Phase 18)

`cost_per_product_researched`, `cost_per_approved_listing`, `cost_per_audited_listing`, `cost_per_completed_order`, `cost_per_inspected_order`, `cost_per_resolved_ticket`, `cost_per_repair`, `cost_per_shop`, `cost_per_employee`, `cost_per_department`, `cost_per_automation_execution`, all split into AI, infrastructure and marketplace fees.

## 5. Actuals

| Month | Infrastructure | AI | Notes |
|---|---|---|---|
| — | — | — | Filled from Phase 1 onward |
