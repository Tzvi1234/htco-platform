# Phase 8A — Browser Infrastructure

| | |
|---|---|
| **ID** | 8A (added 2026-10-23; numbering in [PLAN §2](../PLAN.md#2-phases-and-milestones)) |
| **Milestone** | — |
| **Repositories** | `htco-platform` (most work), `htco-clients` (environment screens) |
| **Size** | L |
| **Depends on** | [Phase 5](phase-05-orders-ingest.md) (gateway, router, Browser interface, Environment Registry), [Phase 7](phase-07-tasks-approvals.md) (Human channel: tasks and approvals). **Starts right after Phase 6, in parallel with Phase 7** (owner, 2026-10-23): until Phase 7's task store lands, Human hand-offs use the relay item of slice 6.10, which Phase 7 then absorbs. The AutoVA recipes (8A.10) are built first, right after the worker, controller, preflight and challenge handler (8A.1 to 8A.4). Slice 8A.6 (Browser Use) is **moved earlier** ([ADR-0025](../../adr/0025-browser-and-network-providers.md) §3). It no longer waits for all of Phase 9, only for the **minimal Model Gateway** pulled forward from slices 9.1 and 9.2: one provider adapter, the pinned `browser-agent-default` alias, hard caps, masking, recording and the kill switch. The secret-injection part of 8A.4 needs [Phase 8B](phase-08b-authenticator.md); until then, login is a Human task. |
| **Parallel with** | Phase 8 and Phase 8B |
| **Decisions** | [ADR-0019](../../adr/0019-unified-execution-engine-and-action-gateway.md), [ADR-0020](../../adr/0020-per-shop-browser-environment.md), [ADR-0022](../../adr/0022-etsy-write-tools-and-browser-scope.md), [ADR-0025](../../adr/0025-browser-and-network-providers.md) (browser and network providers, Browser Use earlier, Valkey), [Etsy automation approval](../../architecture/etsy-automation-approval.md) |
| **Status** | IN PROGRESS (8A.1 built) |

## Goal

A shop's owner can create an optional, honest Browser Environment for the shop. HTCO keeps it signed in and healthy, and runs **only approved, flagged browser actions** through the same Action Gateway as every API call:

- deterministic Playwright recipes for automations;
- Browser Use OSS for employees, with Jev for small proven decisions.

The environment uses one engine per profile and a fixed, truthful egress. Security challenges go to a person, results are structured and validated, and everything is monitored. The first production use is read-only exports that Etsy's API lacks (stats and ads CSVs, scope rows B-02 and B-03), and only once their scope is recorded.

## Why now

Phases 5 and 7 provided the contracts and the Human channel. Listing Health (11), Product Studio (12), finance (13) and messaging (14) need browser work for the gaps in the API. Building the browser channel once, before them, avoids four separate browser integrations.

## Scope

**In:**

- the `browser-worker` component;
- Chromium through Playwright persistent contexts (`launch_persistent_context`, per-shop `user_data_dir`) or a CDP-controlled Chromium;
- sealed profile storage;
- the Browser Controller with Postgres advisory locks;
- preflight and session checks, network health and the challenge handler;
- the recipe catalog (deterministic Playwright) and structured extraction (Pydantic, then Postgres);
- the Browser Use OSS driver for employees;
- prompt-injection guards;
- the browser DLQ and recovery;
- monitoring: structlog, Prometheus and Grafana OSS;
- environment screens;
- the first two recipes (B-02 and B-03, behind flags);
- from ADR-0025:
  - the generic `BrowserProvider` port, with plain Chromium (the code of 8A.1 and 8A.2) as its only implementation;
  - the `NetworkProvider` port: shared egress by default, an optional dedicated egress per shop, consistency checks;
  - Browser Use through the minimal Model Gateway;
  - Valkey only behind the `RateLimiter` or `Cache` ports, and only where Postgres falls short.

**Out:**

- anti-detect browsers, fingerprint spoofing, residential, ISP or rotating proxies, and CAPTCHA solving: never ([ADR-0020](../../adr/0020-per-shop-browser-environment.md));
- any AdsPower or other anti-detect adapter. AdsPower Professional (SunBrowser) is recorded as the owner's chosen external browser provider: an **external dependency** behind the `BrowserProvider` port that this team does not design, specify or build. It stays disabled unless it passes the port contract and every security gate, which today's rules forbid ([ADR-0025](../../adr/0025-browser-and-network-providers.md) §1, "Scope boundary");
- TOTP enrolment and display (8B);
- browser **write** actions on Etsy (B-05 to B-10: Phases 13 and 14, after written confirmation);
- non-Etsy sites (POD partners, suppliers: Phase 13, with their own scope rows).

## Owner prerequisites

- Confirm that AutoVA's terms allow reading his own account (A-01) and approving quotes (A-02) with a browser (A-08), and send a sample AutoVA Export file.
- Fill in the scope rows B-01, B-02 and B-03 in [etsy-automation-approval.md](../../architecture/etsy-automation-approval.md). The written confirmation is recommended first ([D-16](../../product/ASSUMPTIONS.md#1-owner-decisions-needed)).
- Approve the `browser-worker` cost (see Cost impact) and the fixed egress option ([D-18](../../product/ASSUMPTIONS.md#1-owner-decisions-needed)).
- Consent text for storing the shop's Etsy password (ADR-0020 rule 6), approved by the owner.

## Slices

### 8A.1 — Browser worker and sealed profiles

- **The `browser-worker` component:** its own image layer with Chromium and Playwright, its own database role (`htco_browser`: environments, its own jobs, browser artifacts), no Etsy OAuth tokens, and a read-only root file system apart from a tmpfs for unpacked profiles.
- **Profiles:** sealed archives (`browser-profiles/{org}/{shop}/{environment}`), encrypted with envelope keys, with associated data `org|shop|environment`. Updated 2026-10-25 (main's decision): the archives live in a Postgres table that only `htco_browser` reaches, not in Supabase Storage, so the browser worker never holds a project-wide Storage key; each archive is capped at 25 MiB.
- **The `EnvironmentProvisioner` implementation:** create the profile, then a first health check.

| ID | Acceptance criterion |
|---|---|
| AC-8A.1.1 | A profile archive read straight from storage is ciphertext; a copy moved to another shop's path fails to open. |
| AC-8A.1.2 | The browser worker's role cannot read `shop_integrations`, tokens or another organization's environments (pgTAP). |
| AC-8A.1.3 | After a session, nothing of the profile remains on disk outside the sealed archive (file system test). |

**As built (slice 8A.1):** migration `20261025170000_browser_worker_role.sql`, pgTAP `28_browser_worker_role`.

- **The role.** `htco_browser` (no login, no inherit) reaches one organization at a time through `htco.organization_id`. It reads its environments' status and profile columns (never the consent record), writes their profile columns and its archives, and inserts audit rows. It has no right on any other table: no `shop_integrations`, no tokens, no queue, no outbox (the pgTAP test lists every table it can touch). Status transitions, the queue and browser artifacts are granted with the slices that use them (8A.2 to 8A.4).
- **The archives.** `app.browser_profile_archives`: one row per environment, bound to the environment, shop and organization by a composite foreign key, sealed bytes only, 1 byte to 25 MiB. RLS on; no client role and not the agent-worker can read it.
- **Sealing.** `EnvelopeSealer.seal_blob` and `open_blob` (`htco_platform.crypto`): one self-describing blob of version, key ID, wrapped data key and ciphertext. The browser worker has its **own key ring**, separate from the Etsy token vault's (ADR-0020 §8); its setting arrives with the browser worker's composition root (8A.2).
- **The workspace.** `htco_application.browser_profiles.ProfileWorkspaces.checkout` unpacks the archive into a fresh `0700` directory under the worker's tmpfs root, re-seals it on a clean exit, and always wipes it. A failed session keeps the last good archive. Packing keeps regular files and directories only, without Chromium's caches, links, sockets and `Singleton*` lock files; unpacking refuses links, devices, absolute or `..` paths, more than 20,000 entries and more than 256 MiB unpacked.
- **The provisioner.** `SealedProfileProvisioner` seals an empty profile for a `PROVISIONING` environment, records its storage key and key ID, then runs the first health check. Until 8A.3 provides that check, a new profile ends `LOGIN_REQUIRED` (`new_profile_signed_out`), which is true: a new profile is signed out.
- **Not yet.** The `browser-worker` image (Chromium and Playwright, read-only root, tmpfs) comes with the controller in 8A.2, which adds the Playwright dependency.

### 8A.2 — Browser Controller and profile locks

`BrowserController.session`:

- a `pg_try_advisory_lock` on the environment ID, held on a dedicated connection for the session;
- one engine per profile;
- a deadline;
- a `finally` that always cleans up: close, re-seal, wipe temp files and secrets, release the lock.

The controller can fall back from Playwright to Browser Use only for employee actions whose binding allows it.

| ID | Acceptance criterion |
|---|---|
| AC-8A.2.1 | Two concurrent sessions for one environment never both start: the second waits up to its deadline or is `DEFERRED`. Sessions for two shops run in parallel. |
| AC-8A.2.2 | Killing the worker mid-session releases the lock (the connection drops), and the next session re-seals from the last good archive. |
| AC-8A.2.3 | A session past its deadline is closed and recorded as `FAILED (timeout)`; the environment's error counter increments. |

**8A.2a — The `BrowserProvider` port** ([ADR-0025](../../adr/0025-browser-and-network-providers.md) §1).
- The port has `create_profile`, `open`, `connect_cdp`, `health_check`, `save_session`, `close` and status management. The contract is in [execution-engine.md](../../architecture/execution-engine.md).
- Its first and only implementation, `ChromiumProvider`, is **the existing code mapped onto the port, not rewritten**: `SealedProfileProvisioner` and `ProfileWorkspaces` (8A.1), and the controller's write-back and `PlaywrightEngine` (8A.2).
- `BrowserController` stays the only entry point. It holds the lock, the deadline and the record, and it calls the provider inside them.
- `shop_browser_environments.provider` names each environment's provider (default `chromium`). Only the Owner changes it.
- No AdsPower or anti-detect adapter is designed or built. Any external adapter must pass the port contract and every security gate before it can be enabled.

| ID | Acceptance criterion |
|---|---|
| AC-8A.2.4 | One contract suite for `BrowserProvider` (create, open, CDP attach, health, save, close and status, against fixtures and `127.0.0.1` pages only) passes for `ChromiumProvider` and for a fake provider. Nothing in LangGraph, the execution engine, the Action Gateway, the recipes or the guards imports a provider: an architecture test checks it. |
| AC-8A.2.5 | `connect_cdp` exposes CDP only inside the browser worker, as a pipe or loopback in its own network namespace. A test shows the endpoint can't be reached from outside the worker, and that no port is published. |
| AC-8A.2.6 | An environment's provider can be changed only by the Owner, with `aal2` and step-up, and only to a provider whose readiness is `enabled`. The change is audited. An Admin, an agent and an automation are refused (negative tests). Only `chromium` is `enabled` at the end of this phase. |

### 8A.3 — Preflight, session validation and network health

- **Preflight:** Chromium starts, the disk has space, DNS and TLS to the target work, the egress IP equals the configured one, and a JavaScript and resource probe page loads.
- **Session validation:** the signed-in marker for the site.
- **Network diagnostics (daily):** egress stability, latency and a reputation lookup of our own egress IP. These are recorded as diagnostics only and never used to switch IPs.
- **Status updates:** `READY` ↔ `DEGRADED` ↔ `LOGIN_REQUIRED`.
- **The `NetworkProvider` port** ([ADR-0025](../../adr/0025-browser-and-network-providers.md) §2): `egress_for(shop)`, `allocate` and `release` (owner-only, optional) and `check`.
  - The first adapter, `SharedEgress`, gives every shop the shared browser egress. A new shop never needs an allocation (D-18 (c)).
  - A **dedicated egress** per shop is an optional, owner-allocated static datacenter IP of HTCO's own cloud account. It is truthful, never rotated, and never used to hide that shops are related.
  - No residential, ISP or rotating proxy, proxy marketplace or VPN, ever.

| ID | Acceptance criterion |
|---|---|
| AC-8A.3.1 | A wrong egress IP fails preflight, and the session does not start. |
| AC-8A.3.2 | A signed-out profile moves the environment to `LOGIN_REQUIRED` and opens one Human task "sign in to {shop}". |
| AC-8A.3.3 | Two failed health checks in a row set `DEGRADED`; two passing ones restore `READY`. |
| AC-8A.3.4 | Before every session, the public IP seen by an HTCO-controlled echo endpoint equals the shop's expected egress: shared or dedicated, from `NetworkProvider.egress_for`. A mismatch refuses the session with `egress_mismatch` before any page of the target loads. It is recorded and alerts the Owner. |
| AC-8A.3.5 | A shop with a dedicated egress never falls back to the shared egress. With its egress unavailable, the session is refused. A shop with no allocation always gets the shared egress, and creating a shop needs no allocation. |
| AC-8A.3.6 | Every session records the egress ID it used. Two sessions of one shop with different egress IDs and no audited owner change in between raise a consistency finding. |
| AC-8A.3.7 | Allocating, changing or releasing a shop's egress is Owner-only (`aal2`, step-up) and audited. Agents and automations have no tool for it (negative tests). A dependency and configuration test finds no proxy-service or VPN client in the browser worker. |

### 8A.4 — Security challenge handler and human handoff

- **Detectors:** CAPTCHA widgets, passkey and WebAuthn prompts, "verify it's you" pages, block or "unusual activity" pages, and permission errors. Each has recorded fixtures.
- **On detection:** stop, record a screenshot **hash** (the image itself is kept 7 days for the reviewer only), open a Human task with a link, and move the environment to `LOGIN_REQUIRED` or `DEGRADED`. Two challenges in 24 hours move it to `SUSPENDED`.
- **Manual login:** the person signs in through a supervised live view of the same profile (a noVNC or CDP screencast session that only the Owner or Admin can open), then the engine resumes from its checkpoint.
- **With 8B:** TOTP injection from the Secret Broker for TOTP prompts only.

| ID | Acceptance criterion |
|---|---|
| AC-8A.4.1 | Each challenge fixture stops the run within one step, with zero automatic retries, and one Human task. |
| AC-8A.4.2 | The second challenge in 24 hours suspends the environment and switches its `browser.etsy.*` flags off for that shop. |
| AC-8A.4.3 | After a person completes the login, the waiting execution resumes exactly once. |

### 8A.5 — Recipes, structured extraction and result verification

- **The recipe catalog** (`registry/browser-recipes/*.toml`, reviewed data). Each recipe has an ID, site, scope row (for Etsy), allowed URL patterns, allowed actions (click, type, download), its Playwright steps, an output Pydantic model and a verification rule.
- **Extraction:** Playwright reads the DOM or the downloaded file; Pydantic validates it; Postgres stores it in typed tables with source, `observed_at` and the recipe version.
- **The first recipes:** B-02 (stats export, to `listing_metrics` and `shop_traffic_daily`) and B-03 (ads report, to `ads_daily`). Both are behind their flags, and both are read-only.
- **AutoVA (owner priority, 2026-10-23):** A-01 reads an order's status and tracking number from the owner's own AutoVA account (read-only; the input of the Phase 6 relay, slice 6.10). B-12 marks an Etsy order as shipped with that tracking number in Shop Manager, only when the API shipment write is blocked (A-04), with the owner's approval each time. A-01 is built right after the controller and the challenge handler, before B-02 and B-03. Updated 2026-10-23: AutoVA signs in with email and password and has no TOTP, so A-01 needs the Secret Broker (5.17) but not the Authenticator (8B). A-01 and A-02 are specified in slice 8A.10 and in the [AutoVA integration](../../architecture/autova-integration.md) page.

| ID | Acceptance criterion |
|---|---|
| AC-8A.5.1 | A recipe navigates only to its allowed URL patterns; any other navigation aborts the run. |
| AC-8A.5.2 | An export that fails its schema stores nothing and opens a quality finding; one that passes is idempotent per shop and day. |
| AC-8A.5.3 | A recipe whose scope row is missing, or whose flag is off, is refused before the controller starts. |

### 8A.6 — Browser Use driver for employees (moved earlier, ADR-0025 §3)

- [Browser Use](https://github.com/browser-use/browser-use) (MIT) is wrapped behind `BrowserSession`. It is the **primary AI browser engine** for employees, attached through `BrowserProvider.connect_cdp`.
- **The order inside a session:**
  1. Playwright for every deterministic step (recipes, extraction, verification).
  2. Jev "Ultrafast" as an optional fast path for small structured decisions, only once its 9.6 benchmark passes. It is off before Phase 9.
  3. Then Browser Use, within the recipe's allowed actions and URL allowlist, with the result validated by the recipe's schema.
- It is used only by employees, never by automations (ADR-0019 rule 4).
- **It no longer waits for all of Phase 9.** It needs only the **minimal Model Gateway**, pulled forward from 9.1 and 9.2. Until the Phase 9 router exists, its model calls stay safe in these ways:
  - **Gateway only.** Browser Use's model client implements its LLM interface and calls HTCO's `ModelGateway` port. No provider SDK is used.
  - **No provider keys in the browser worker.** The gateway runs in the agent worker and is reached over the internal network with a short-lived token per run (execution, employee, shop, alias). The browser worker's egress allowlist blocks provider API hosts.
  - **A fixed catalog alias**, `browser-agent-default`, pinned under ADR-0023's rules, with an empty fallback list: an unavailable model stops the run and hands it to a person.
  - **Hard caps.** Cost, tokens and steps are capped per run, and cost per shop per day. Each call is reserved before it is sent and settled after, on a minimal Postgres ledger that 9.4 absorbs.
  - **PII masking.** DOM and accessibility text is masked for DC1. Screenshots are sent only for pages whose recipe declares no buyer data.
  - **Recording and kill switches.** Every call is recorded. Kill switches act globally, per shop, per employee and per alias, plus the provider kill switch.

| ID | Acceptance criterion |
|---|---|
| AC-8A.6.1 | Every Browser Use model call appears in `routing_decisions` and `model_usage` with the employee and task. |
| AC-8A.6.2 | An automation actor requesting a non-deterministic binding is refused (repeats AC-5.13.2 end to end). |
| AC-8A.6.3 | Browser Use's model client calls only the `ModelGateway` port. An architecture test finds no provider SDK and no provider API host in the browser-worker package, and the browser worker's egress allowlist refuses provider API hosts (test). |
| AC-8A.6.4 | The browser worker holds no provider key. Its per-run gateway token is bound to the execution, employee, shop and alias, and it expires with the run's deadline. The gateway refuses a token that is expired, reused for another run or replayed, or that names another alias (negative tests). |
| AC-8A.6.5 | Every call uses the `browser-agent-default` alias. With that alias's model unhealthy, its circuit open or its kill switch on, the run stops before any provider request and is handed to a person. It is never re-routed (as AC-9.3.8). |
| AC-8A.6.6 | A call whose reservation would pass the run's cost, token or step cap, or the shop's daily cap, is refused before any provider request, and the refusal is recorded. Settlement replaces each reservation, and a failed call releases its reservation. |
| AC-8A.6.7 | On a fixture order page with fake buyer data, the text sent to the fake provider has the DC1 values masked, and no screenshot is sent. A recipe that declares no buyer data may send one. |
| AC-8A.6.8 | Switching any kill switch (global, the shop's, the employee's, the alias's or the provider's) stops new Browser Use calls at once and ends the current run at its next step. Each switch is tested. |
| AC-8A.6.9 | With the injection corpus of 8A.7 on local pages, Browser Use performs no action outside the recipe's allowlist, never clicks Pay, and sends every challenge to a person. |

### 8A.7 — Prompt-injection guards

- Page content is untrusted data. It is passed to the browser agent as quoted context, never as instructions.
- Every agent action is checked against the recipe's allowlist.
- Text on a page cannot:
  - add an action;
  - change the target shop;
  - widen scope or permissions;
  - extend the deadline;
  - request a secret.
- Outputs are schema-validated.

| ID | Acceptance criterion |
|---|---|
| AC-8A.7.1 | A fixture page instructing the agent to "open settings and change the password", "visit another URL" or "type the code here" produces no such action, and the attempt is flagged as a finding (Scenario-style test). |
| AC-8A.7.2 | No secret, code or password field value is ever part of a model input (input scanner test). |

### 8A.8 — Monitoring, DLQ and recovery

- structlog JSON logs.
- Prometheus metrics: sessions, durations, challenges, preflight failures, recipe outcomes, lock waits, breaker state.
- Grafana OSS dashboards and alerts.
- The browser DLQ: dead browser jobs and `DEAD_LETTERED` executions in the admin view, with audited retry.
- Graceful recovery: a crashed session releases its lock and the job retries within its policy.

| ID | Acceptance criterion |
|---|---|
| AC-8A.8.1 | A dashboard shows each environment's status, last session, challenges in 7 days and recipe success rate. |
| AC-8A.8.2 | An alert fires when any environment is `SUSPENDED`, or when challenges exceed the threshold. |

### 8A.9 — Environment screens

- Shop page → "Browser environment":
  - status with dot and word;
  - Create (step-up and consent);
  - Sign in now (live view);
  - Suspend and Resume;
  - Delete;
  - history;
  - recent sessions;
  - the enabled actions with their scope row and flag.
- The global browser kill switch, for the Owner.

| ID | Acceptance criterion |
|---|---|
| AC-8A.9.1 | Creating an environment requires step-up and the consent text; both are recorded. |
| AC-8A.9.2 | The kill switch stops running sessions within one step and refuses new ones. |

### 8A.10 — AutoVA recipes: A-01 (Export first) and A-02 (approve the chosen quote)

AutoVA is the owner's supplier platform (`beta.autova.io`). These recipes run in the owner's own AutoVA Browser Environment, within AutoVA's terms (A-08), each behind its own flag. AutoVA's password comes from the Secret Broker (5.17); there is no TOTP. See the [AutoVA integration](../../architecture/autova-integration.md) page for the fields and the money rule.

- **A-01 — Orders and tracking (read).** It runs on a schedule (every 30 minutes by default) and whenever the tracking relay (6.10) asks.
  1. **Export first:** press Export, download the file, parse it, validate every row (Pydantic), and upsert `autova_orders` snapshots with `source = autova_export`.
  2. **Fallback:** if the export fails or fails its schema, read the Orders page table (`source = autova_screen`).

  Its output feeds:
  - the tracking relay (carrier and tracking number);
  - the order monitor (Unpaid past its window → one alert and a Human task; HTCO never pays);
  - the profit field (Phase 13 cross-check).
- **A-02 — Approve the chosen quote and confirm the order (write).** It runs only for an Owner-approved quote (slice 7.17, policy W3). Steps:
  1. open the quote by its AutoVA reference;
  2. check that the supplier, price and terms on the page equal the approved quote, and stop if they differ;
  3. approve that quote and confirm the order.

  The allowed actions exclude every payment control. A payment step on the page stops the run and hands it to the owner. The result is verified by A-01 showing the order as confirmed, and only then is the supplier mapping recorded.

| ID | Acceptance criterion |
|---|---|
| AC-8A.10.1 | With a valid export, A-01 stores one snapshot per AutoVA order and never reads the screen; with a broken export, it falls back to the screen once and records the fallback. |
| AC-8A.10.2 | A status that moves backwards (for example Shipped → Confirmed) is stored as a drift with a finding, never applied. |
| AC-8A.10.3 | A-02 refuses to act when the page's supplier, price or terms differ from the approved quote's hash, and opens a Human task. |
| AC-8A.10.4 | A fixture page with a Pay button: A-02 never clicks it (the allowlist test), and a payment step stops the run with a Human task. |
| AC-8A.10.5 | An AutoVA order still Unpaid after its window raises exactly one alert and one task for the owner; HTCO takes no payment action. |

## Test plan

- Unit tests for the controller, router and guards with fakes.
- Recorded-page fixtures for challenges and injections, served locally.
- Integration tests with a real Chromium against a local test site (never etsy.com in CI).
- pgTAP for the roles.
- A manual verification on the owner's shop with B-02 only, after its scope row and flag.
- The `BrowserProvider` contract suite against `ChromiumProvider` and a fake provider.
- Egress consistency tests with a local echo endpoint.
- Browser Use against a fake `ModelGateway` that records every call: caps, masking, kill switches and the stop on an unavailable alias.
- Tests for the per-run gateway token: expired, replayed, wrong alias.

## Security checklist

- [ ] No stealth plug-ins, fingerprint changes, proxies or CAPTCHA services in the dependency tree (dependency allowlist test).
- [ ] The browser worker holds no OAuth tokens; profiles are sealed; secrets only through grants.
- [ ] THREAT_MODEL updated (browser environment assets, profile theft, injection, session hijack).
- [ ] The browser worker holds no provider key. Browser Use's model calls go only through the Model Gateway, with a per-run token, the pinned alias, hard caps, masking, recording and kill switches.
- [ ] Only `chromium` is an enabled provider. An external adapter, including the owner's recorded AdsPower dependency, stays disabled unless it passes the port contract and every security gate (ADR-0025 §1).
- [ ] Egress is truthful: shared, or a dedicated HTCO-owned static IP, never rotated and never hiding shop linkage. Changes are Owner-only and audited, and consistency is checked before every session.
- [ ] CDP is reachable only inside the browser worker.

## Demo

1. Create an environment for the pilot shop and sign in through the live view.
2. Run the stats export (B-02) and show the validated rows.
3. Show a challenge fixture handed to a person.
4. Show the Grafana dashboard.

## Exit gate

- [ ] All AC-8A pass; the dependency allowlist test passes.
- [ ] B-02 ran in production for one week with zero challenges, or every challenge was handled by a person.
- [ ] The owner approved the environment for the pilot shop.

## Rollback

Switch the global browser kill switch on: all `browser.etsy.*` flags go off, and environments go to `SUSPENDED`. The API, Internal and Human channels keep working, and the browser-worker component can be scaled to zero.

## Risks

| Risk | Mitigation |
|---|---|
| Etsy restricts the account despite the verbal approval | Read-only exports first; written confirmation before any write ([D-16](../../product/ASSUMPTIONS.md#1-owner-decisions-needed)); the kill switch; a fallback to a person |
| Frequent challenges make the environment useless | Persistent profiles and human pace; measure the challenge rate weekly; stop if it exceeds 1 per shop per week |
| Etsy's page layout changes break recipes | Schema validation stores nothing wrong; recipe failures alert; recipes are versioned |
| Browser Use spends too much on models | Hard caps per run and per shop per day, reserved before every call (AC-8A.6.6), until the Phase 9 ledger takes over; deterministic recipes preferred |
| An external browser provider weakens the honest design | Only `chromium` is enabled; an external adapter needs the port contract and every security gate, and today's gates forbid anti-detect providers, so enabling one needs a superseding ADR with the risks accepted on record ([ADR-0025](../../adr/0025-browser-and-network-providers.md) §1) |
| A shop's egress changes unnoticed | Consistency check before every session; the egress ID recorded per session; owner-only, audited changes (AC-8A.3.4 to AC-8A.3.7) |

## Cost impact

See [COST_MODEL §6](../../cost/COST_MODEL.md#6-architecture-upgrade-2026-10-23-execution-browser-authenticator):

- `browser-worker` about $12–24/month (Chromium needs 1–2 GB);
- a fixed egress IP $0 to $25/month, depending on the option;
- Grafana OSS self-hosted $0, or Grafana Cloud free tier;
- Browser Use model calls from the employee budgets.
