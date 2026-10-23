# Phase 8A — Browser Infrastructure

| | |
|---|---|
| **ID** | 8A (added 2026-10-23; numbering in [PLAN §2](../PLAN.md#2-phases-and-milestones)) |
| **Milestone** | — |
| **Repositories** | `htco-platform` (most work), `htco-clients` (environment screens) |
| **Size** | L |
| **Depends on** | [Phase 5](phase-05-orders-ingest.md) (gateway, router, Browser interface, Environment Registry), [Phase 7](phase-07-tasks-approvals.md) (Human channel: tasks and approvals). **Starts right after Phase 6, in parallel with Phase 7** (owner, 2026-10-23): until Phase 7's task store lands, Human hand-offs use the relay item of slice 6.10, which Phase 7 then absorbs. The AutoVA recipes (8A.10) are built first, right after the worker, controller, preflight and challenge handler (8A.1 to 8A.4). Slice 8A.6 (Browser Use) also needs [Phase 9](phase-09-router-advisor.md) (model gateway, budgets). The secret-injection part of 8A.4 needs [Phase 8B](phase-08b-authenticator.md); until then, login is a Human task. |
| **Parallel with** | Phase 8 and Phase 8B |
| **Decisions** | [ADR-0019](../../adr/0019-unified-execution-engine-and-action-gateway.md), [ADR-0020](../../adr/0020-per-shop-browser-environment.md), [ADR-0022](../../adr/0022-etsy-write-tools-and-browser-scope.md), [Etsy automation approval](../../architecture/etsy-automation-approval.md) |
| **Status** | PLANNED |

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
- the first two recipes (B-02 and B-03, behind flags).

**Out:**

- anti-detect browsers, fingerprint spoofing, residential, ISP or rotating proxies, and CAPTCHA solving: never ([ADR-0020](../../adr/0020-per-shop-browser-environment.md));
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
- **Profiles:** sealed archives in Supabase Storage (`browser-profiles/{org}/{shop}/`), encrypted with the vault's envelope keys, with associated data `org|shop|environment`.
- **The `EnvironmentProvisioner` implementation:** create the profile, then a first health check.

| ID | Acceptance criterion |
|---|---|
| AC-8A.1.1 | A profile archive read straight from storage is ciphertext; a copy moved to another shop's path fails to open. |
| AC-8A.1.2 | The browser worker's role cannot read `shop_integrations`, tokens or another organization's environments (pgTAP). |
| AC-8A.1.3 | After a session, nothing of the profile remains on disk outside the sealed archive (file system test). |

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

### 8A.3 — Preflight, session validation and network health

- **Preflight:** Chromium starts, the disk has space, DNS and TLS to the target work, the egress IP equals the configured one, and a JavaScript and resource probe page loads.
- **Session validation:** the signed-in marker for the site.
- **Network diagnostics (daily):** egress stability, latency and a reputation lookup of our own egress IP. These are recorded as diagnostics only and never used to switch IPs.
- **Status updates:** `READY` ↔ `DEGRADED` ↔ `LOGIN_REQUIRED`.

| ID | Acceptance criterion |
|---|---|
| AC-8A.3.1 | A wrong egress IP fails preflight, and the session does not start. |
| AC-8A.3.2 | A signed-out profile moves the environment to `LOGIN_REQUIRED` and opens one Human task "sign in to {shop}". |
| AC-8A.3.3 | Two failed health checks in a row set `DEGRADED`; two passing ones restore `READY`. |

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

### 8A.6 — Browser Use driver for employees (needs Phase 9)

- [Browser Use](https://github.com/browser-use/browser-use) (MIT), wrapped behind `BrowserSession`.
- Its model calls go through HTCO's model gateway, under the employee's routing policy and budget (Jev "Ultrafast" for small decisions only where its benchmark passed, ADR-0015).
- It uses the recipe's allowed actions and URL allowlist.
- It is used only by employees, never by automations (ADR-0019 rule 4).

| ID | Acceptance criterion |
|---|---|
| AC-8A.6.1 | Every Browser Use model call appears in `routing_decisions` and `model_usage` with the employee and task. |
| AC-8A.6.2 | An automation actor requesting a non-deterministic binding is refused (repeats AC-5.13.2 end to end). |

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

## Security checklist

- [ ] No stealth plug-ins, fingerprint changes, proxies or CAPTCHA services in the dependency tree (dependency allowlist test).
- [ ] The browser worker holds no OAuth tokens; profiles are sealed; secrets only through grants.
- [ ] THREAT_MODEL updated (browser environment assets, profile theft, injection, session hijack).

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
| Browser Use spends too much on models | Employee budgets (Phase 9); deterministic recipes preferred |

## Cost impact

See [COST_MODEL §6](../../cost/COST_MODEL.md#6-architecture-upgrade-2026-10-23-execution-browser-authenticator):

- `browser-worker` about $12–24/month (Chromium needs 1–2 GB);
- a fixed egress IP $0 to $25/month, depending on the option;
- Grafana OSS self-hosted $0, or Grafana Cloud free tier;
- Browser Use model calls from the employee budgets.
