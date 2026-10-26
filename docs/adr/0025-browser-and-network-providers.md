# ADR-0025: A layered `BrowserProvider` contract with one managed Chromium implementation, browser work dispatched to the browser worker, honest configuration profiles, an optional per-shop egress, and Valkey only where Postgres falls short

- **Status:** Accepted (owner's decisions, 2026-10-26)
- **Date:** 2026-10-26
- **Deciders:** Owner, engineering
- **Amends:**
  - [ADR-0019](0019-unified-execution-engine-and-action-gateway.md): the Browser channel dispatches its work to the browser worker and returns a waiting outcome, `DISPATCHED`, instead of awaiting the browser inline (§3).
  - [ADR-0020](0020-per-shop-browser-environment.md): the engine sits behind a layered `BrowserProvider` contract (§1), each environment has a validated configuration profile (§2), and a shop may have its own fixed egress (§4).
- **Related:** [ADR-0004](0004-model-gateway.md), [ADR-0021](0021-authenticator-service.md), [ADR-0022](0022-etsy-write-tools-and-browser-scope.md), [ADR-0023](0023-routing-modes-and-difficulty-assessment.md), [execution-engine.md](../architecture/execution-engine.md), [Phase 8A](../plan/phases/phase-08a-browser-infrastructure.md), [etsy-multi-shop-policy.md](../architecture/etsy-multi-shop-policy.md), D-18, D-20, the architecture audit of 2026-10-26 (board #134: AUD-04, AUD-05, AUD-13)

## Context

What is built:
- **Slice 8A.1** built sealed per-shop profiles: `ProfileWorkspaces`, `SealedProfileProvisioner` and `app.browser_profile_archives`.
- **Slice 8A.2** is in review: the Browser Controller with profile locks and deadlines (#121), and the Playwright engine in the browser-worker package (#122).

Three gaps remain:
- **No replaceable engine, one fixed IP.** Nothing names the engine as a replaceable part, and ADR-0020 writes a single fixed IP into its rules.
- **Inline browser calls (AUD-04).** The gateway awaits every channel inline. A browser recipe takes minutes and runs in another process, so an inline await would hold the automation's lease and the concurrency slot for the whole session. It cannot cross the process boundary at all.
- **Sync ports, missing import contracts.** The ports the application awaits are synchronous (AUD-05), and no import contract keeps Playwright, Browser Use or the model SDKs where they belong (AUD-13).

The owner decided on 2026-10-26:
1. a generic browser provider contract, split into layers with a capabilities declaration, and one implementation, a managed Chromium;
2. per-shop network behind its own port, with an optional fixed egress per shop;
3. Browser Use sooner than "after Phase 9";
4. Browser Configuration Profiles, within the scope in §2;
5. Valkey in the plan, but only where Postgres does not already cover the need.

## Decision drivers

- One port per concern, the existing code as its implementation, and no duplicate infrastructure.
- **Business logic never depends on Chromium.** LangGraph, the execution engine, the Action Gateway, the recipes and the guards see ports only.
- Every security control of ADR-0019, ADR-0020 and ADR-0022 stays where it is:
  - the Action Gateway;
  - scope rows and flags;
  - the Secret Broker;
  - challenges go to a person;
  - no Pay click;
  - recipe allowlists;
  - sealed profiles;
  - the browser worker's own database login (#116).
- The browser stays honest: its real User-Agent, the server's time zone, and no fingerprint changes.
- No model call outside the Model Gateway, even before the Phase 9 router exists.
- Postgres stays the system of record. Valkey never holds durable state.

## Options considered

1. **Keep the engine and the egress as fixed code** (ADR-0020 as written). Nothing to test a replacement against, and the inline browser call of AUD-04 stays.
2. **A layered provider contract with one managed Chromium implementation, dispatched browser work, and honest configuration profiles.** **Adopted.**
3. **Providers that attach to browsers run outside HTCO, or adapters for external vendors.** Not designed or built. Any future provider must implement the contract and pass the same gates (§1).

## Decision

### 1. `BrowserProvider`: one contract in seven layers

`htco_application.browser.BrowserProvider` is a set of async `Protocol`s, one per layer, plus a capabilities declaration. The signatures are in [execution-engine.md](../architecture/execution-engine.md) §3.

| Layer | What it covers | `ManagedChromiumProvider`, the only implementation |
|---|---|---|
| **Provisioning** | The profile manager: create, re-provision and destroy a persistent, isolated per-shop profile, and validate a configuration before use (§2) | `SealedProfileProvisioner` and `ProfileWorkspaces` (8A.1) |
| **Lifecycle** | `open`; `close`, always, which wipes the workspace; a fenced `save_session`; `renew`, a short scheduled session that checks the sign-in and re-seals refreshed cookies so that sessions last; `recover`, which restores the last good archive after a crash or corruption, then runs a health check. Status changes go only through `ShopEnvironmentRegistry.transition` and its table | The controller's session and fenced write-back (#121, `still_held`) |
| **Connection** | The connection that Playwright, Browser Use and Jev attach to: CDP over a pipe, or loopback inside the browser worker's own network namespace. It is never published | The persistent context's own connection; `--remote-debugging-pipe` for a raw CDP client |
| **Execution** | The engines that act on a page (§5): Playwright for deterministic steps, the only engine automations use (ADR-0019 rule 4); Browser Use, the primary AI engine, for employees; Jev, for small proven decisions | `PlaywrightEngine` (#122); Browser Use (8A.6); Jev after its 9.6 benchmark |
| **Authentication** | The sign-in state, the login steps and the challenge stop. The session code redeems one-use grants: the password from the Secret Broker, the TOTP code from the Authenticator (ADR-0021). It types each into the recipe's declared field. Passkeys, CAPTCHAs and "verify it's you" pages go to a person | The 8A.4 detectors; the grant is redeemed by the session, never by the provider |
| **Networking** | All traffic through the egress that `NetworkProvider` names for the shop (§4), and the observed IP for the consistency check | The browser worker's network path. No proxy setting is accepted from anywhere else |
| **Monitoring** | Preflight and health reports, the engine version, crash and resource events, metrics | The 8A.3 checks and the 8A.8 metrics |

**The capabilities declaration** (`ProviderCapabilities`, a frozen value) says what a provider supports:
- its ID and engine, and the pinned engine version;
- the layers it implements and its execution engines;
- its CDP transport (`pipe` or `loopback`);
- whether it supports `renew`, `recover` and downloads;
- how many profiles it runs in parallel per worker.

The controller refuses a recipe that needs a capability the provider does not declare, before any session starts. A declaration is a claim that the contract suite checks. It grants nothing.

**Shared contract tests.** One suite, `tests/contracts/browser_provider/`, runs layer by layer against fixtures and `127.0.0.1` pages only, never a live site. Every provider must pass it. It passes for `ManagedChromiumProvider` and for a fake provider. It checks that:
- every declared capability is really there;
- the honesty rules of §2 hold in a real browser;
- CDP cannot be reached from outside the worker;
- two profiles run in parallel, each under its own lock, and one profile is never open twice;
- a challenge stops the run with no retry;
- no secret reaches a log or a model input.

**Rules for the port:**
- **`BrowserController` remains the only entry point for a session.** It holds the profile lock, the deadline and the session record, and calls the provider inside them. Business logic depends on `BrowserController` and `BrowserSession`, never on a provider and never on Chromium.
- **Import contracts (AUD-13).**
  - `playwright` and `browser_use` can be imported only in the browser-worker package (`htco_browser_worker`), and the model SDKs only in the gateway package. import-linter enforces both.
  - An architecture test checks that no provider class is imported by the domain, the application layer, the graphs, the execution engine, the gateway, the recipes or the guards.
- **Async ports (AUD-05).** Every layer is async, and so are `NetworkProvider`, the configuration store and the dispatch ports of §3. Each adapter owns its thread hop through the one persistence helper, so no blocking database call runs on the event loop.
- **A provider receives only what the port passes.** No OAuth token, no Secret Broker access, no database role and no network outside its egress. It gets a secret only as a one-use value for one declared field, and never stores, logs or returns it.
- **Parallel profiles.** A browser worker runs several sessions at once, one per profile, up to the declared parallel capacity. Each session holds its own profile lock on its own connection. The same profile is never open twice (ADR-0020 rule 3).
- **A fenced write-back.** `save_session`, `renew` and `recover` take the session's own `ProfileFence`: the lease of its own profile lock, never a lookup by environment. Two things make that safe:
  - The lease is checked on that lock's own connection. A session whose connection dropped can therefore never pass through the connection of the session that took the lock next. That is the #121 finding.
  - The archive is written on that same connection, or with a generation compare-and-set on `browser_profile_archives`. That closes the window between checking the lock and writing.

  A session that lost its lock never writes the profile back.
- **Choosing the provider.** Each environment names its provider in `shop_browser_environments.provider` (`managed-chromium`). Only the Owner changes it, with `aal2` and step-up, audited, and only to a provider whose readiness is `enabled`.
- **Future providers.** No other provider is designed or built here: none that attaches to a browser run outside HTCO's browser worker, and no external-vendor adapter. **Any future provider must implement this contract and pass the same gates:** the shared contract suite, the SECURITY GATE checklist on board #95, and ADR-0020's rules. The owner's recorded choice of an external provider is in D-20: out of scope, and not enabled.

### 2. Browser Configuration Profiles

Each environment has one **configuration profile**: a versioned record of how its browser runs. The owner approved this scope on 2026-10-26.

**The same for every shop.** This is the platform baseline, not editable per shop:
- **One pinned Chromium.**
  - Every browser worker runs the one Chromium build that matches the pinned Playwright, in an image pinned by digest. There is never a per-shop version.
  - Updates follow a controlled policy: Renovate after the seven-day rule, then the contract suite and a canary health check, then the whole fleet at once.
  - A profile last opened by a newer build is refused, because Chromium profiles cannot be downgraded. For rollback, each profile's pre-update archive is kept until the new build has run cleanly for 7 days.
- **Operational settings:**
  - one color scheme;
  - one viewport, sized for Browser Use and Playwright (for example 1280 × 900);
  - one permissions policy. It denies notifications, camera, microphone, clipboard reads and geolocation. Downloads go only into the session's own folder, and only for a recipe that declares one.
- **Standard privacy and security defaults:**
  - the sandbox and site isolation stay on;
  - no extensions;
  - Chromium's password manager, autofill, sync and translation are off, because secrets come only from the Secret Broker;
  - no crash or usage reports;
  - CDP is never on a TCP port (§1).
- **Environment health checks.** The 8A.3 preflight and session checks run against the configuration in use.

**Set per shop**, and validated on every save:
- **Locale and language:** the language the shop is really operated in, from the shop's recorded operating languages (for example `he-IL` or `en-US`). It sets the browser's locale and its `Accept-Language`.
- **The egress:** the shop's `NetworkProvider` assignment, either shared or its own dedicated egress (§4).
- **The persistent profile:** the user-data directory, with its cookies, storage and sessions. This is unchanged from 8A.1 and 8A.2.

**Never configurable.** The browser stays honest (ADR-0020 §2):
- no per-shop User-Agent: the browser sends its real one;
- no per-shop screen resolution: one viewport for every shop;
- no per-shop time zone: the browser worker's own;
- no geolocation, unless a real, authorized need is recorded for a recipe in its scope row. Even then it is the truthful location, never one chosen to make a shop look elsewhere;
- no fingerprint, "stealth" or automation-hiding setting, no extension, and no proxy setting outside `NetworkProvider`.

So shops never look like different devices. Two shops' sessions report the same User-Agent, platform, time zone and viewport. They differ only in language, where the shops really work in different languages, and in egress, where a shop has its own.

**Settings must not conflict.** The validator runs when a profile is saved and again when a session starts. It refuses:
- any never-configurable key;
- a locale outside the shop's recorded languages;
- a dedicated egress that has not been allocated;
- a recipe that needs a download or a permission that the baseline denies;
- a Chromium build that differs from the fleet's pinned one.

Each refusal names its issue, and nothing is saved or started.

**Changes and records:**
- Each change is a new version. The Owner or an Admin makes it, with step-up, and it is audited. Changes to the egress and the provider stay Owner-only.
- Agents and automations have no tool for any of this.
- Every session records the configuration version, the Chromium version and the egress ID it used. Two sessions that differ, with no audited change in between, raise a consistency finding.

**The central management screen** (web first; slice 8A.9) lists every shop environment the viewer may see. Each row shows the status, the configuration version, the Chromium version, the egress, the last health check and the last session. From there an Owner or Admin can:
- create an environment, with step-up and consent;
- edit its configuration, which is validated before saving;
- check it now;
- provision, re-provision or recover it.

Long operations run as browser jobs (§3), never inline in the API.

### 3. Browser work is dispatched, not awaited (amends ADR-0019; AUD-04)

The gateway runs steps 1 to 6 as today: validate, authorize, apply the approval rule, claim idempotency, route and guard. For a Browser binding, step 7 changes:

1. **Dispatch.** The agent-worker's `BrowserChannel` opens no browser. In one transaction it moves the action execution to `DISPATCHED`, enqueues one `browser.run` job and writes `actions.dispatched.v1` to the outbox.
   - The job's idempotency key is the action execution and its attempt.
   - Its concurrency key is the profile (`browser:{environment_id}`, one run at a time).
   - Its payload is the execution ID and attempt, the environment, the recipe and its version, the scope ID, and only the parameters the recipe declares. It never carries a secret, a token or any authority.

   `submit` returns `DISPATCHED`. It is a waiting outcome: `StatusKind.waiting`, with the reason `waiting_for_browser`.
2. **Nothing is held while the browser runs.**
   - An automation execution moves to a waiting step and gives up its job lease, as it does for an approval.
   - An employee's graph is interrupted at a checkpoint.
   - The gateway's concurrency slot is released at dispatch, and the `browser:{shop}` rate budget is spent there.
3. **The browser worker runs it.** It runs in its own process, under its own database login (`htco_browser`, #116), and claims only `browser.*` jobs. Before it touches a page, it checks again at execution time:
   - the claim is still `DISPATCHED`, with the same request hash;
   - the deadline has not passed;
   - the environment is `READY`;
   - the recipe's flag is on and its scope row is recorded;
   - no kill switch is on: global, the shop's, or the action's (the execution-time switch of #126).

   If any check fails, the run ends `refused` with its code, and no page is touched. Otherwise the browser worker runs the recipe through `BrowserController.session` and validates the result against the recipe's schema. Browser Use runs only for employee bindings with `deterministic = False`.
4. **It publishes the outcome** in one transaction, idempotent per execution and attempt:
   - one `browser_run_outcomes` row with the status (`succeeded`, `failed`, `challenge`, `refused` or `timeout`), the validated output, the evidence hashes and the error code;
   - `browser.run_finished.v1` in the outbox.

   The row never holds a secret, and holds DC1 only when the recipe's output model declares it.
5. **Resume through `submit`.** A `browser.resume` job in the agent-worker wakes the waiting execution, or resumes the employee's graph. It resubmits the same request with the same idempotency key. `submit` finds the stored claim in `DISPATCHED` and resumes it, as it already resumes a human hand-off:
   - with no outcome yet, it answers `DISPATCHED` again;
   - with an outcome, it validates the output against the action's output schema and runs the action's verification (the schema, or a re-read through the API channel). Then it records exactly one final result: `SUCCEEDED`, `FAILED`, or `HANDED_TO_HUMAN` with one Human task for a challenge.

   The audit record, the activity record and `actions.executed.v1` follow, as for any action. A resume never runs the browser step again.
6. **Lost runs.**
   - A read recipe whose run died is retried within its policy.
   - A write is never re-sent blindly. A write run with no outcome after its deadline and a grace period becomes `HANDED_TO_HUMAN`, with one task: "check whether it happened".
7. **The browser worker decides nothing about authority.** Authorization and approvals stay in the gateway. The browser worker only re-checks what can stop work.
   - Its role can claim `browser.*` jobs, read a claim's state, request hash and deadline in its own organization, and insert outcomes.
   - It cannot write `action_executions` or approvals.
   - A forged or replayed job is refused: one for an execution that is not `DISPATCHED`, or that carries another request hash.

### 4. `NetworkProvider`: an optional per-shop egress, no fixed vendor

`htco_application.browser.NetworkProvider` is an async `Protocol`:
- `egress_for(shop)` returns the shop's `EgressProfile`: `shared` or `dedicated`, an egress ID and the expected public IP;
- `allocate(shop, *, by)` and `release(shop, *, by)` are optional, Owner-only (`aal2`, step-up) and audited;
- `check(shop)` runs the egress consistency check below.

The rules:
- **Shared by default.** A new shop **never** needs an allocation. Without one, it uses the **shared browser egress**, the platform's own fixed IP (D-18 (c) today).
- **An optional dedicated egress.** The plan supports a **fixed, separate egress IP per shop**. It is truthful and HTCO-controlled: a static datacenter IP of our own cloud account, in the shop's region, such as a small droplet that HTCO runs. It is never rotated.
- **No disguise, ever.** As in ADR-0020: no residential, ISP or rotating proxies, no proxy marketplaces, no third-party proxies and no VPNs. A per-shop egress exists for isolation, stability and allowlisting, **never to hide that shops are related** (D-18). The shops stay declared as related wherever Etsy asks.
- **No fixed vendor.** The first adapter, `SharedEgress`, returns the shared egress for every shop. A dedicated-egress adapter is added behind the same port when the owner first allocates one.

**Consistency checks** (8A.3; AC-8A.3.4 to AC-8A.3.7):
- **Before every session,** the observed public IP must equal the shop's expected egress. It is read from an echo endpoint that HTCO controls.
- **No silent fallback.** A shop with a dedicated egress never falls back to the shared one. A mismatch refuses the session (`egress_mismatch`), records it and alerts the Owner.
- **Changes are owner actions.** A shop's egress changes only by an owner action, and the change is audited. Each session records the egress ID it used.
- **A daily diagnostic** keeps checking stability, latency and the reputation of our own IPs. As before, it never switches IPs.

### 5. Browser Use as the primary AI engine, moved earlier

**The engines inside a session:**
1. **Playwright** runs every deterministic step: recipes, extraction and verification. Automations only ever use Playwright (ADR-0019 rule 4).
2. **Jev "Ultrafast"** is an optional fast path for small structured decisions. Examples are which allowed step comes next, or a yes or no on a validated page state. It is used for a family of decisions only after its 9.6 benchmark passes for that family, and it is off before Phase 9.
3. **Browser Use** is the primary AI engine, for open-ended navigation by **employees**, inside the recipe's allowed actions and URL allowlist. It attaches through the Connection layer, and the recipe's schema still validates its result.

8A.6 no longer waits for the whole of Phase 9. It needs only the **minimal Model Gateway** below, pulled forward from 9.1 and 9.2.

**How Browser Use's model calls stay safe before the Phase 9 router:**
- **The gateway only.** Browser Use gets a model client that implements its LLM interface and calls HTCO's `ModelGateway` port, never a provider SDK.
- **No provider keys in the browser worker.**
  - The browser worker reaches the gateway in the agent worker over the internal network.
  - It authenticates with a short-lived token per run, bound to the execution, the employee, the shop and the alias. The token carries no budget or authority of its own.
  - The browser worker's egress allowlist blocks provider API hosts, so a misconfiguration cannot bypass the gateway.
- **A fixed catalog alias, pinned.** Calls use an alias such as `browser-agent-default`, never a model ID, under ADR-0023's pinned semantics. Its fallback list is empty, so an unavailable model **stops** the run and hands it to a person. Nothing is re-routed.
- **Hard caps, checked before each call.**
  - Each run has a cap on cost, tokens and steps, and each shop has a daily cap.
  - Budgets are reserved before the call and settled after it, on a minimal Postgres ledger. 9.4's full ledger later absorbs it unchanged.
  - The run's deadline stays the controller's.
- **PII masking before anything leaves.** DOM and accessibility-tree text is masked for DC1 (buyer names and addresses). Screenshots are sent only for pages whose recipe declares no buyer data; otherwise the model gets the masked text only.
- **Recording.** Every call writes one routing decision and one usage row, with the execution, the employee, the shop, the alias and the cost (AC-8A.6.1).
- **Kill switches.** There are five: global, per shop, per employee, per alias, and the provider kill switch (AC-9.1.4).
- **Prompt injection.** Page content is data (8A.7): it is quoted and schema-validated, and it never changes the task, the allowlist or the grants. There is no Pay click, and CAPTCHAs and challenges go to a person.

### 6. Valkey, only where Postgres does not cover the need

Each coordination need the existing ports cover today, checked one by one:

| Need | Port | Postgres covers it? | Valkey? |
|---|---|---|---|
| Sign-in throttling | `RateLimiter` | **Yes:** `rate_windows` with atomic reservation (#47) | No |
| Profile locks (one engine per profile) | `ProfileLocks` | **Yes:** advisory locks on a dedicated connection, released on disconnect (8A.2) | No. Correctness rests on Postgres's lock and the fencing check |
| Job queue, scheduling, browser dispatch and resume (§3) | `JobQueue`, outbox | **Yes:** `SKIP LOCKED` (ADR-0007) and the outbox | No |
| Idempotency, budgets, approvals | gateway, ledger | **Yes**, and they must stay transactional | **Never:** durable state |
| Concurrency slots (gateway guards, browser sessions per worker) | `guards.slots` | **Yes:** slot rows and advisory locks | No |
| Live UI updates | notifications | **Yes:** Supabase Realtime and `LISTEN/NOTIFY` | No |
| **Etsy's shared per-second limit across processes** | `RateLimiter` (Etsy) | **Partly.** Today the limit is per process (`_wait_for_slot`), with the daily quota taken from Etsy's headers. A Postgres window per second works for one API and one worker, but means row contention at a few calls per second from several processes | **Yes, when** the measured contention or the process count passes the threshold set at the 8A.8 or Phase 10 review: an atomic `INCR` with a TTL per second, behind the same port |
| Short-lived response caches shared across processes (dashboard reads, Etsy GETs) | a `Cache` port (new, optional) | In-process caches are enough today | **Optional,** only if measured duplicate reads justify it |

Valkey is **planned, but not needed now**:
- **Where:** behind the existing `RateLimiter` port, and an optional `Cache` port, once the threshold is reached.
- **How it runs:** DigitalOcean's managed Valkey, in the same region, with TLS and authentication. Keys are namespaced per organization and expire.
- **What it never holds:** buyer data, secrets, or any state whose loss would change a result. Losing Valkey may slow HTCO down, but it never breaks it, and the Postgres fallback stays in the code.

### 7. What stays as planned

These are unchanged:
- the Authenticator and TOTP (8B, ADR-0021);
- secrets through the Secret Broker;
- extraction and verification (8A.5);
- the prompt-injection guards (8A.7);
- monitoring, the DLQ and recovery (8A.8);
- the Phase 5 contracts, the Action Gateway and its controls;
- the scope rows of etsy-automation-approval.md;
- the browser database login of #116: the worker's own role, set before any composition root.

## Consequences

- **Positive.**
  - Any provider meets one contract, tested layer by layer, and business logic never touches Chromium.
  - Browser work crosses the process boundary without holding a lease or a slot, and a lost write is never re-sent.
  - Shops' browsers are configured centrally and honestly, and conflicting settings are caught before a session starts.
  - Browser Use arrives months earlier, and no model call escapes the gateway.
  - No new infrastructure is added until it is measured to be needed.
- **Negative.**
  - New moving parts:
    - the dispatch job, the outcome row and the resume path (one more waiting state);
    - the interim budget ledger, which 9.4 absorbs;
    - the CDP endpoint, kept to a pipe or loopback inside the worker.
  - One Chromium build for the whole fleet means an update cannot be staged shop by shop. The contract suite, the canary and the kept pre-update archives cover that.
  - A shop's language sets its `Accept-Language`, which Etsy sees. It is true: the shop really works in that language.
  - Not adopted now: per-shop container isolation, one container per shop's sessions (owner, 2026-10-26). It remains a possible later hardening option. Today shops are separated by their own profiles, locks, configuration and egress inside the browser worker.
- **Follow-ups:**
  - 8A.2a: the contract, `ManagedChromiumProvider`, the import contracts and the async ports;
  - 8A.2b: configuration profiles and the pinned Chromium;
  - 8A.2c: dispatch and resume;
  - 8A.3: the egress acceptance criteria;
  - 8A.6: moved earlier, with the minimal gateway from 9.1 and 9.2;
  - 8A.9: the central management screen;
  - the Valkey review at 8A.8 or Phase 10.

## Validation

The acceptance criteria are in Phase 8A:
- AC-8A.2.4 to AC-8A.2.18: the contract, configuration profiles, dispatch and resume;
- AC-8A.3.4 to AC-8A.3.7: egress;
- AC-8A.6.3 to AC-8A.6.9: Browser Use through the gateway;
- AC-8A.9.3 and AC-8A.9.4: the management screen.

## Validation log

- 2026-10-26: Decisions approved by the owner:
  - a layered `BrowserProvider` contract with a capabilities declaration and shared contract tests, and `ManagedChromiumProvider` as its only implementation;
  - Browser Configuration Profiles, within the approved scope;
  - an optional `NetworkProvider` egress per shop;
  - browser work dispatched to the browser worker, with resume through `submit` (AUD-04);
  - Browser Use earlier, through the gateway;
  - Valkey only where Postgres falls short.

  No provider that attaches to browsers run outside HTCO, and no external-vendor adapter.
