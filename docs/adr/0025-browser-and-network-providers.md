# ADR-0025: Browser and network providers behind ports, Browser Use earlier through the gateway, and Valkey only where Postgres falls short

- **Status:** Accepted (owner's decisions, 2026-10-26)
- **Date:** 2026-10-26
- **Deciders:** Owner, engineering
- **Amends:** [ADR-0020](0020-per-shop-browser-environment.md). The browser engine sits behind a `BrowserProvider` port, the egress behind a `NetworkProvider` port, and a shop may have its own fixed egress (§2).
- **Related:** [ADR-0004](0004-model-gateway.md), [ADR-0019](0019-unified-execution-engine-and-action-gateway.md), [ADR-0021](0021-authenticator-service.md), [ADR-0022](0022-etsy-write-tools-and-browser-scope.md), [ADR-0023](0023-routing-modes-and-difficulty-assessment.md), [execution-engine.md](../architecture/execution-engine.md), [Phase 8A](../plan/phases/phase-08a-browser-infrastructure.md), [etsy-multi-shop-policy.md](../architecture/etsy-multi-shop-policy.md), D-18

## Context

Slices 8A.1 and 8A.2 already built the plain-Chromium environment:
- sealed per-shop profiles (`ProfileWorkspaces`, `SealedProfileProvisioner`, `app.browser_profile_archives`);
- the Browser Controller with profile locks and deadlines (`BrowserController`, `PostgresProfileLocks`, `app.browser_sessions`);
- the Playwright engine in its own package (`htco_browser_worker.engine.PlaywrightEngine`).

Nothing yet names the engine as a replaceable part, and the egress is a single fixed IP written into ADR-0020's rules. The owner wants four things:
1. the engine behind a generic port, so a provider can be swapped without touching LangGraph, the execution engine or the security layers;
2. per-shop network as a port of its own, with an optional fixed egress per shop;
3. Browser Use sooner than 8A.6's "after Phase 9";
4. Valkey in the plan, but only where Postgres does not already cover the need.

## Decision drivers

- One port per concern, with the existing code as the first adapter and no duplicate infrastructure.
- Every security control of ADR-0019, ADR-0020 and ADR-0022 stays exactly where it is:
  - the Action Gateway;
  - scope rows and flags;
  - the Secret Broker;
  - CAPTCHAs to a person;
  - no Pay click;
  - recipe allowlists;
  - sealed profiles;
  - the dedicated database login of #116.
- No model call outside the Model Gateway, even before the Phase 9 router exists.
- Postgres stays the system of record. Valkey is never added for durable state.

## Decision

### 1. `BrowserProvider`: the generic browser port

`htco_application.browser.BrowserProvider` is a `Protocol`. Its contract is in [execution-engine.md](../architecture/execution-engine.md) §3.

| Operation | What it does | First adapter: plain Chromium (what 8A.1 and 8A.2 already built) |
|---|---|---|
| `create_profile(environment)` | A new, empty, sealed profile for the environment | `SealedProfileProvisioner.provision` → `ProfileWorkspaces.create` |
| `open(environment, *, deadline)` | Starts one engine on the unpacked profile, returns a handle | `ProfileWorkspaces.checkout` + `PlaywrightEngine.launch` (persistent context) |
| `connect_cdp(handle)` | A CDP endpoint for Playwright or Browser Use to attach to, reachable **only inside the browser worker** (a pipe, or loopback in its own network namespace; never published) | The persistent context's own connection; `--remote-debugging-pipe` where a raw CDP client is needed |
| `health_check(handle)` | Preflight and session validation (8A.3) | The 8A.3 checks |
| `save_session(handle)` | Seals the profile back, only while the profile lock is held, with a fencing check | The controller's write-back, after `still_held` (#121) |
| `close(handle)` | Closes the engine and wipes the workspace, always | The session's `finally` path |
| `status(environment)`, `set_status(...)` | Reads or moves the environment's status (`PROVISIONING`, `READY`, `DEGRADED`, `LOGIN_REQUIRED`, `LOCKED`, `DISABLED`) under the existing transition rules | `ShopEnvironmentRegistry.transition` (8A.3, 8A.4) |

Rules for the port:
- **`BrowserController` remains the only entry point for a session.** It holds the profile lock and the deadline and records the session, and it calls the provider inside. LangGraph, the execution engine, the Action Gateway, the recipes and the guards depend on `BrowserController` and `BrowserSession`, never on a provider.
- **A provider is chosen per environment** (`shop_browser_environments.provider`, default `chromium`). Only the Owner can change it: `aal2` with step-up, audited.
- **A provider never receives anything the port does not pass.** That means no OAuth token, no Secret Broker access (secrets are injected by the session through grants, ADR-0021), no database role and no network outside its `NetworkProvider` egress.
- **A provider can be enabled only when** it passes the port's contract suite (run against fixtures and local pages only, never a live site) and every security gate that applies to the Browser channel: the SECURITY GATE checklist on board #95 (item 8 in particular), ADR-0020's honesty rules, the dependency allowlist, and THREAT_MODEL.

**The only implementation is plain Chromium** with persistent, isolated per-shop profiles, which is the code of 8A.1 and 8A.2 mapped onto the port. **We do not design or build any AdsPower or anti-detect adapter.**

#### Scope boundary: the owner's external provider

The owner has chosen **AdsPower Professional (SunBrowser)** as his browser provider. It is recorded here as an **EXTERNAL dependency**: an "AdsPower adapter" behind the generic `BrowserProvider` port.

- **Out of scope for this team.** We do not design, specify or build it. This ADR and the plan contain no integration details for it: no API calls, settings or configuration.
- **Our deliverable is unchanged:**
  - the generic `BrowserProvider` port;
  - the plain-Chromium implementation, the only active one;
  - the `NetworkProvider` port (§2).
- **It must pass the same gates as any adapter.** It must satisfy the port contract and pass the same security gates before it can be enabled for any shop.
- **As the rules stand today, it cannot pass.** ADR-0020 rejected anti-detect browsers and fingerprint spoofing as tools for evading account security and linkage. Etsy's API Terms forbid "circumventing security features" ([multi-shop policy](../architecture/etsy-multi-shop-policy.md)), and SECURITY GATE item 8 forbids anti-detect software, fingerprint spoofing and proxies.
- **Recording is not enabling.** Recording the dependency enables nothing. Enabling it for a shop needs a separate owner decision in a new ADR. That ADR has to change those rules on purpose and record the risks it accepts: Etsy's terms, and the account standing of the shop and of every shop linked to it. Until then, plain Chromium is the only enabled provider, and the dependency allowlist test (8A security checklist) keeps anti-detect packages out of our tree.

### 2. `NetworkProvider`: per-shop network and egress, no fixed vendor

`htco_application.browser.NetworkProvider` is a `Protocol`:
- `egress_for(shop)` returns the shop's `EgressProfile`: `shared` or `dedicated`, an egress ID and the expected public IP;
- `allocate(shop, *, by)` and `release(shop, *, by)` are owner-only, audited, and optional;
- `check(shop)` runs the egress consistency check below.

- **Shared by default.** A new shop **never** needs an allocation. Without one, it uses the **shared browser egress**, the platform's own fixed IP (D-18 (c) today).
- **An optional dedicated egress.** The plan supports a **fixed, separate egress IP per shop**. It is truthful and HTCO-controlled: a static datacenter IP of our own cloud account in the shop's region, such as a reserved IP on a droplet that HTCO runs. It is never rotated.
- **No disguise, ever.** As in ADR-0020: no residential, ISP or rotating proxies, no proxy marketplaces and no VPNs. A per-shop egress exists for isolation, stability and allowlisting, **never to hide that shops are related** (D-18). The shops remain declared as related wherever Etsy asks.
- **No fixed vendor.** The first adapter, `SharedEgress`, returns the shared egress for every shop. A dedicated-egress adapter is added when the owner first allocates one, behind the same port.

**Consistency checks** (8A.3; AC-8A.3.4 to 8A.3.7):
- Before every session, the observed public IP, read from an echo endpoint HTCO controls, must equal the shop's expected egress.
- A shop with a dedicated egress never falls back to the shared one silently. A mismatch refuses the session (`egress_mismatch`), records it, and alerts the Owner.
- A shop's egress changes only by an owner action, and the change is audited. Sessions record the egress ID they used.
- A daily diagnostic keeps checking stability, latency and our own IP's reputation. As before, it never switches IPs.

### 3. Browser Use as the primary AI browser engine, moved earlier

**The order of engines inside a session:**
1. **Playwright** runs every deterministic step: recipes, extraction and verification. Automations only ever use this (ADR-0019 rule 4).
2. **Jev "Ultrafast"** is an optional fast path for small structured decisions, such as which allowed step comes next, or yes or no on a validated page state. It is used only after its 9.6 benchmark passes for that family. Before Phase 9 it is off.
3. **Browser Use** is the primary engine for open-ended navigation by **employees**, inside the recipe's allowed actions and URL allowlist. It attaches through `connect_cdp`, and the result is still validated by the recipe's schema.

8A.6 no longer waits for the whole of Phase 9. It needs only the **minimal Model Gateway** below, pulled forward from 9.1 and 9.2.

**How Browser Use's model calls stay safe before the Phase 9 router.** This builds on the owner's proposal:
- **The gateway only.** Browser Use gets a model client that implements its LLM interface and calls HTCO's `ModelGateway` port, never a provider SDK.
- **No provider keys in the browser worker.** It reaches the gateway in the agent worker over the internal network, authenticated by a short-lived, per-run token bound to the execution, the employee, the shop and the alias. The token carries no budget or authority of its own. The browser worker's egress allowlist blocks provider API hosts, so a misconfiguration cannot bypass the gateway.
- **A fixed catalog alias, pinned.** For example `browser-agent-default`, never a model ID. ADR-0023's pinned semantics apply. Its fallback list is empty, so an unavailable model **stops** the run and hands it to a person; nothing is re-routed.
- **Hard caps, checked before each call.** Each run has a cap on cost, tokens and steps, and so does each shop per day. Budgets are reserved before the call and settled after it on a minimal Postgres ledger, which 9.4's full ledger later absorbs unchanged. The run's deadline stays the controller's.
- **PII masking before anything leaves.**
  - DOM and accessibility-tree text is masked for DC1 (buyer names and addresses).
  - Screenshots are sent only for pages whose recipe declares no buyer data.
  - Otherwise the model gets the masked text only.
- **Recording.** Every call writes one routing decision and one usage row, with the execution, employee, shop, alias and cost (AC-8A.6.1).
- **Kill switches:** global, per shop, per employee and per alias, and the provider kill switch (AC-9.1.4).
- **Prompt injection.** Page content is data (8A.7): it is quoted and schema-validated, and it never changes the task, the allowlist or the grants. There is no Pay click, and CAPTCHAs and challenges go to a person.

### 4. Valkey, only where Postgres does not cover the need

These are the coordination needs the existing ports cover today, checked one by one:

| Need | Port | Postgres covers it? | Valkey? |
|---|---|---|---|
| Sign-in throttling | `RateLimiter` | **Yes**: `rate_windows` with atomic reservation (#47) | No |
| Profile locks (one engine per profile) | `ProfileLocks` | **Yes**: advisory locks on a dedicated connection, released on disconnect (8A.2) | No. Correctness depends on Postgres's lock and the fencing check |
| Job queue, scheduling | `JobQueue` | **Yes**: `SKIP LOCKED` (ADR-0007) | No |
| Idempotency, budgets, approvals | gateway, ledger | **Yes**, and they must stay transactional | **Never**: durable state |
| Concurrency slots (gateway guards, browser sessions per worker) | `guards.slots` | **Yes**: slot rows and advisory locks | No |
| Live UI updates | notifications | **Yes**: Supabase Realtime and `LISTEN/NOTIFY` | No |
| **Etsy's shared per-second limit across processes** | `RateLimiter` (Etsy) | **Partly**: today it is per process (`_wait_for_slot`), with the daily quota from Etsy's headers. A Postgres window per second works for one API and one worker, but means row contention at a few calls per second from several processes | **Yes, when** the measured contention or the process count passes the threshold set at the 8A.8 or Phase 10 review: an atomic `INCR` with a TTL per second, behind the same port |
| Short-lived response caches shared across processes (dashboard reads, Etsy GETs) | a `Cache` port (new, optional) | In-process caches are enough today | **Optional**, only if measured duplicate reads justify it |

So Valkey is **planned, not needed now**. It goes behind the existing `RateLimiter` port, and an optional `Cache` port, when the threshold is reached. It runs as DigitalOcean's managed Valkey in the same region, with TLS and authentication. Keys are namespaced per organization and expire. It never holds buyer data, secrets or state whose loss would change a result: losing Valkey may slow HTCO down, never break it. The Postgres fallback stays in the code.

### 5. What stays as planned

These are unchanged:
- the Authenticator and TOTP (8B, ADR-0021);
- secrets through the Secret Broker;
- extraction and verification (8A.5);
- the prompt-injection guards (8A.7);
- monitoring, the DLQ and recovery (8A.8);
- the Phase 5 contracts, the Action Gateway and its controls;
- the scope rows of etsy-automation-approval.md;
- the browser database login of #116, the worker's own role, set before any composition root.

## Consequences

- **Positive.**
  - The engine, the egress and the cache can each be swapped behind a port, with the security layers untouched.
  - Browser Use arrives months earlier, without a model call escaping the gateway.
  - No new infrastructure until it is measured to be needed.
- **Negative.**
  - The interim budget ledger is new code. 9.4 absorbs it.
  - The CDP endpoint is a new internal surface, kept to a pipe or loopback inside the worker.
  - The owner's external provider stays recorded but unenabled until a superseding ADR, which is a known gap between his choice and what the gates allow.
- **Follow-ups:**
  - 8A.2a, the ports and the Chromium mapping;
  - 8A.3, the egress ACs;
  - 8A.6, moved earlier, with the minimal gateway from 9.1 and 9.2;
  - the Valkey review at 8A.8 or Phase 10.

## Validation

The acceptance criteria are AC-8A.2.4 to AC-8A.2.6 (the port and its contract suite), AC-8A.3.4 to AC-8A.3.7 (egress) and AC-8A.6.3 to AC-8A.6.9 (Browser Use through the gateway), in Phase 8A.

## Validation log

- 2026-10-26: Decisions approved by the owner (a generic `BrowserProvider` with plain Chromium as the only implementation; a `NetworkProvider` with an optional dedicated egress; Browser Use earlier through the gateway; Valkey only where Postgres falls short). AdsPower Professional (SunBrowser) recorded as the owner's chosen external provider, out of scope and not enabled (§1).
