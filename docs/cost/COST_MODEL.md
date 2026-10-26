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
| DigitalOcean `web` | 0 | 0 | Static site inside the paid app (client ADR-0006); the deploy refuses a spec over the owner's $20 hard limit (today the API and the agent-worker, $5 each) |
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

## 6. Architecture upgrade (2026-10-23): execution, browser, authenticator

Free or OSS options first. Prices are list prices to re-check on the day a service is created.

| Item | Free or OSS option (first choice) | Paid option, if needed | Monthly cost | When |
|---|---|---|---|---|
| Action Gateway, router, limiter, breakers, retries, validation | aiolimiter (MIT), PyBreaker (BSD), Tenacity (Apache-2.0), Pydantic (MIT), all in existing processes | — | $0 | Phase 5 |
| Queues, coordination, locks, idempotency | Postgres (existing Supabase) and advisory locks | Valkey (BSD; DigitalOcean managed from about $15) only when the 8A.8 or Phase 10 review measures the need ([ADR-0025](../adr/0025-browser-and-network-providers.md) §6) | $0 (Valkey about $15 if ever added) | Phase 5; Valkey conditional |
| `agent-worker` (automations, API and Internal channels) | — | DigitalOcean App Platform worker (PR #54) | $5 (up to $12) | Phase 5 |
| LangGraph Postgres checkpointer | `langgraph-checkpoint-postgres` (MIT) on the existing database | — | $0 | Phase 8 |
| `browser-worker` (Chromium, Playwright, Browser Use) | Playwright (Apache-2.0), Chromium, Browser Use (MIT) | A basic HTCO droplet of 2 GB ($12). App Platform cannot host it: the browser worker's egress rules for Chromium's own OS user need `NET_ADMIN`, which App Platform does not grant, and the worker starts no session without them ([ADR-0025](../adr/0025-browser-and-network-providers.md) §5, AC-8A.2.22) | About $12 | Phase 8A |
| Fixed egress IP for browser workers | The browser droplet's own static IP, included in its price: the shared browser egress becomes that IP (D-18 (a)) | — | $0 | Phase 8A |
| Optional per-shop egress (`NetworkProvider`, [ADR-0025](../adr/0025-browser-and-network-providers.md) §4) | None: shops use the shared browser egress by default | An HTCO-owned static IP per shop, for example a small droplet of our own, allocated by the Owner | About $5 per shop per month when enabled; $0 by default | Phase 8A (planned, owner-allocated) |
| Profile and export storage | Sealed profiles in Postgres (`browser_profile_archives`, up to 25 MiB each; ADR-0020 log, 2026-10-25); exports in Supabase Storage (within the Pro plan's 100 GB) | — | $0 | Phase 8A |
| Monitoring: metrics and dashboards | Prometheus (Apache-2.0) and Grafana OSS (AGPL) self-hosted on the browser droplet, or the Grafana Cloud free tier (10k series, 14-day retention) | Grafana Cloud Pro (from about $19 plus usage) | $0 | Phase 5 (exposure), 8A (dashboards) |
| Logs | structlog to the platform's existing log destinations | Log drain ($60, already listed as optional) | $0 | Existing |
| Authenticator | PyOTP (MIT) on the existing vault | — | $0 | Phase 8B |
| Browser Use model calls | Deterministic Playwright recipes first (no model) | Model calls under each employee's budget (Phase 9) | Within the AI budget (D-06) | Phase 8A |
| **Not used by HTCO** | — | Anti-detect browsers (AdsPower Professional included: recorded in D-20 as the owner's external choice, out of scope and not enabled), third-party, residential, ISP or rotating proxies, hosted "stealth" browsers, CAPTCHA-solving services | $0 (declined, [ADR-0020](../adr/0020-per-shop-browser-environment.md)) | — |

**Pilot total added by the upgrade:** about $17/month: the worker from PR #54 at $5, plus the browser droplet at about $12, whose own IP is the shared browser egress (D-18 (a)). An optional dedicated egress per shop adds about $5 per shop when the Owner enables one.
