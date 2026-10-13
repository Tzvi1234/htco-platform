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
| DigitalOcean `worker` | 5 | 12 | From Phase 5 |
| DigitalOcean `web` | 5 | 5 | Caddy service for security headers (Phase 3) |
| Container registry | 0 | 5 | DigitalOcean Starter or GHCR within GitHub Pro storage |
| Resend | 0 | 20 | Free until inbound mail (Phase 12) |
| GitHub Pro | 4 | 4 | Rulesets on private repositories |
| Error monitoring | 0 | 0 | Free developer tier |
| **Total before AI** | **48** | **87** | |
| Staging (when created) | +15 | +25 | Second Supabase project (Micro, about $10) plus a small app |

Before Phase 5 the bill is smaller (no worker; Supabase from Phase 2). Optional later: point-in-time recovery from about $100/month (needs larger compute), dedicated egress IP up to $25, log drains $60, Windows code signing about $10/month, Google Play developer account $25 one-time.

## 3. Variable AI cost (planning method)

AI cost is computed per unit of work, not per model:

```
cost_per_unit = Σ over model calls ( input_tokens × input_price
                                     + cached_tokens × cache_price
                                     + output_tokens × output_price ) × (1 + retry_rate)
```

| Unit | Model tier (planned) | Planning range per unit | Notes |
|---|---|---|---|
| Listing audit | balanced + vision for photos | $0.02–0.10 | Batch API halves cost for non-urgent audits |
| New listing draft (copy, classification, QA) | balanced, advanced for QA | $0.10–0.50 | Images extra |
| Supplier quote evaluation | rules first; economy for ranking | under $0.01 | Most work is code |
| Incident triage | economy, escalate to balanced | $0.005–0.05 | Only on exceptions |
| Support draft | balanced | $0.01–0.05 | |

Prices per model live in the model catalog with an as-of date (Phase 8), not in this document. Pilot AI spend is expected to stay under the cap the owner sets (decision D-06).

## 4. Unit economics to report (from Phase 11)

`cost_per_listing`, `cost_per_audited_listing`, `cost_per_inspected_order`, `cost_per_resolved_ticket`, `cost_per_repair`, `cost_per_shop`, all split into AI, infrastructure and marketplace fees.

## 5. Actuals

| Month | Infrastructure | AI | Notes |
|---|---|---|---|
| — | — | — | Filled from Phase 1 onward |
