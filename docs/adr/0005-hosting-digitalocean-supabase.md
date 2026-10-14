# ADR-0005: DigitalOcean App Platform and Supabase Pro

- **Status:** Accepted on 2026-10-14: region Frankfurt (DigitalOcean `fra`, Supabase `eu-central-1`); images in GitHub Container Registry (GHCR)
- **Date:** 2026-10-14
- **Deciders:** Owner, engineering

## Context

The original planning brief fixes hosting on DigitalOcean App Platform (managed PaaS, no self-managed servers) and data, auth, storage and queues on Supabase Pro, with no DigitalOcean managed database. Research on 2026-10-14 surfaced constraints that shape the details.

## Facts that shape the decision

- DigitalOcean App Platform **cannot connect to IPv6 hosts**. Supabase direct database connections are IPv6 unless the **IPv4 add-on** ($4 per month) is bought; the Supavisor pooler is IPv4 on all plans.
- Supavisor **transaction mode** (port 6543) does not support prepared statements or pipelining, which the LangGraph checkpointer uses; **session mode** (5432) or a direct connection does.
- DigitalOcean static sites cannot set custom response headers (no CSP or HSTS).
- DigitalOcean cron jobs run at most every 15 minutes; rollbacks restore code and configuration but never the database.
- DigitalOcean's own Dockerfile builder lacks BuildKit features such as cache mounts.
- Supabase Pro ($25) includes a Micro compute instance (60 direct connections, 200 pooler clients), daily backups kept 7 days (database only, not storage objects); point-in-time recovery costs $100+ per month and needs larger compute.

## Decision

- **One DigitalOcean app, `htco-prod`**, with components `api` (service), `web` (service serving the SPA through Caddy with strict headers, or a static site until real data is shown), and `worker` (worker). More components only by ADR.
- **Same region** for DigitalOcean and Supabase. Recommended: Frankfurt (`fra` with Supabase `eu-central-1`), close to the owner and suitable for EU personal data. Alternative: New York (`nyc` with `us-east-1`), closer to Etsy and US buyers.
- **Supabase Pro** with the **IPv4 add-on**, so the API and workers use direct connections with their own pools; the agent runtime uses a direct connection for checkpoints. Without the add-on: session mode for workers and checkpoints, transaction mode with prepared statements off for the API.
- **Images are built once in GitHub Actions**, scanned, pushed to a registry and deployed by digest.
- **Migrations run from CI before deploy**, owner-triggered for production, always backward compatible.
- **Staging** (`htco-staging` app plus a separate Supabase project) is created when integration testing against real accounts needs it, and no later than Phase 20.

## Consequences

- Positive: managed infrastructure, small fixed cost (see [COST_MODEL.md](../cost/COST_MODEL.md)), clear upgrade paths.
- Negative: two providers to monitor; App Platform limits (request timeout around 100 seconds for long-lived connections, no configurable WAF) shape the design of live updates; console users can read secrets, so DigitalOcean team roles must stay tight.
- Follow-ups: connection pool budgets per component in Phase 2; live-update transport decided in Phase 15. Components: `api`, `agent-worker` (automations and AI employees; an `automation-worker` splits out only when measured load or isolation justifies it, [ADR-0012](0012-living-office-organization.md)) and `web`.

## Validation

Phase 1 deploys `/healthz` and `/readyz`; Phase 2 proves the database connection from App Platform; restore and kill-worker drills run before production data is relied on (Phase 5).

## Validation log

- 2026-10-14 — The owner chose Frankfurt and GHCR (decision D-02). Accounts: DigitalOcean team created, payment method pending; Supabase organization exists.
