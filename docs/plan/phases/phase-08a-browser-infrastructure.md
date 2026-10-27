# Phase 8A — Browser Infrastructure

| | |
|---|---|
| **ID** | 8A (added 2026-10-23; numbering in [PLAN §2](../PLAN.md#2-phases-and-milestones)) |
| **Milestone** | — |
| **Repositories** | `htco-platform` (most work), `htco-clients` (environment screens) |
| **Size** | L |
| **Depends on** | [Phase 5](phase-05-orders-ingest.md) (gateway, router, Browser interface, Environment Registry), [Phase 7](phase-07-tasks-approvals.md) (Human channel: tasks and approvals). **Starts right after Phase 6, in parallel with Phase 7** (owner, 2026-10-23): until Phase 7's task store lands, Human hand-offs use the relay item of slice 6.10, which Phase 7 then absorbs. The AutoVA recipes (8A.10) are built first, right after the worker, controller, preflight and challenge handler (8A.1 to 8A.4). Slice 8A.6 (Browser Use) is **moved earlier** ([ADR-0025](../../adr/0025-browser-and-network-providers.md) §5). It no longer waits for all of Phase 9, only for the **minimal Model Gateway** pulled forward from slices 9.1 and 9.2: one provider adapter, the pinned `browser-agent-default` alias, hard caps, masking, recording and the kill switch. The secret-injection part of 8A.4 needs [Phase 8B](phase-08b-authenticator.md); until then, login is a Human task. |
| **Parallel with** | Phase 8 and Phase 8B |
| **Decisions** | [ADR-0019](../../adr/0019-unified-execution-engine-and-action-gateway.md), [ADR-0020](../../adr/0020-per-shop-browser-environment.md), [ADR-0022](../../adr/0022-etsy-write-tools-and-browser-scope.md), [ADR-0025](../../adr/0025-browser-and-network-providers.md) (browser and network providers, Browser Use earlier, Valkey), [Etsy automation approval](../../architecture/etsy-automation-approval.md) |
| **Status** | IN PROGRESS (8A.1 and the 8A.2 controller built) |

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
  - the layered `BrowserProvider` contract with a capabilities declaration and one shared contract suite, and `ManagedChromiumProvider` (the code of 8A.1 and 8A.2) as its only implementation;
  - Browser Configuration Profiles: one pinned Chromium for the fleet, the same operational settings for every shop, locale and language per shop, validated and versioned, with a central management screen;
  - browser work dispatched to the browser worker, with resume through `submit` (audit AUD-04);
  - async ports (AUD-05) and the import contracts that keep Playwright, Browser Use and the model SDKs in their packages (AUD-13);
  - the `NetworkProvider` port: shared egress by default, an optional dedicated egress per shop, consistency checks;
  - Browser Use through the minimal Model Gateway;
  - Valkey only behind the `RateLimiter` or `Cache` ports, and only where Postgres falls short.

**Out:**

- anti-detect browsers, fingerprint spoofing, residential, ISP or rotating proxies, and CAPTCHA solving: never ([ADR-0020](../../adr/0020-per-shop-browser-environment.md));
- any other browser provider: none that attaches to a browser run outside HTCO's browser worker, and no external-vendor adapter. Any future provider must implement the contract and pass the same gates ([ADR-0025](../../adr/0025-browser-and-network-providers.md) §1);
- a per-shop User-Agent, screen resolution, time zone or geolocation, or anything else that would make shops look like different devices (ADR-0025 §2);
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

**As built (slice 8A.2, the controller):** migration `20261025190000_browser_sessions.sql`, pgTAP `30_browser_sessions`.

- **`htco_application.browser_controller.BrowserController.session`.** It refuses a suspended, failed or deleted environment (`SessionRefusedError`) and takes the profile lock (`ProfileLocks`). It then unpacks the profile through 8A.1's workspace, starts one engine (`BrowserEngine`) and enforces the deadline with `asyncio.timeout`. The application layer imports the standard library only. Cleanup always runs to its end, even under cancellation: the engine closes, the profile is re-sealed on a clean exit only and wiped always, and the lock is released.
- **Session outcomes.** `succeeded`; `failed` with the code `timeout`, `error` or `lock_lost`, which also increments the environment's error counter; or `deferred` when another session held the profile past the deadline (`SessionDeferredError`). A `TimeoutError` raised by the work itself is an `error`, not the deadline. The deadline is checked again right before the engine starts, so a slow unpack never begins a launch past it.
- **The lease fence.** `ProfileLocks.hold()` yields a `ProfileLease` bound to its own lock; in Postgres that is the dedicated connection that took the advisory lock. Before the clean exit re-seals the profile, the controller asks `still_held(lease)` about its **own** lease. A session whose lock connection dropped therefore fails as `lock_lost` and never writes back, even after another session has taken the environment's lock. Leaving a lease never releases a later holder's lock. **The fenced write (#169, ADR-0025's fenced `save_session`).** The re-seal passes the session's lease to `ProfileArchiveStore.put(archive, fence=lease)`. In Postgres the write runs on the lease's own dedicated connection, in one transaction as `htco_browser` in the tenant's scope (both `set local`), and checks first that this backend still holds the advisory lock. An exiting backend never commits, and no other session can take the lock while it lives, so the check is part of the write: a lease lost at any point before the commit raises `ProfileFenceLostError`, writes nothing, and the session fails as `lock_lost`. This blocked enabling 8A until it merged.
- **Destroying a profile.** `BrowserController.destroy_profile` deletes the archive under the same profile lock, so a session that is still running cannot write the profile back afterwards.
- **`htco_persistence.browser_sessions`.** `PostgresProfileLocks` polls `pg_try_advisory_lock(hashtextextended('htco.browser_profile:<environment>', 0))` on a dedicated connection until the deadline. A killed worker's connection drops, which releases the lock. `PostgresSessionLog` writes `app.browser_sessions` and the environment's `error_count` and `last_session_at` on the browser role.
- **`app.browser_sessions`.** Purpose and failure are codes, never free text, and an ended session is final. The browser role may insert and end its organization's sessions; the Owner and Admins read theirs. The browser role's only new environment column right is `error_count`; it still cannot change a status.
- **The engine (second PR).** `htco_browser_worker.engine.PlaywrightEngine`, in the new `workers/browser` package, opens `chromium.launch_persistent_context` on the unpacked profile. It passes only headless, the Chromium sandbox, a launch timeout and `accept_downloads=False`, and a test pins that list: no user agent, proxy, switches, locale, time zone or viewport. `playwright==1.64.0` is pinned exactly. Import-linter keeps Playwright inside the browser worker and keeps the browser worker away from the Etsy adapters and token modules. The dependency allowlist test pins the browser worker's whole resolved closure in `uv.lock` to a reviewed list, so any new package (a stealth, proxy or CAPTCHA one included) fails until a reviewer adds it; a denylist checks the whole lock as well. Launching and closing Chromium run to their end even when the session is cancelled, so the lock is never released under a live browser. CI's `browser` job runs a real Chromium against a page on 127.0.0.1: a cookie set in one session survives into the next through the sealed profile.
- **Still to come:** the `browser-worker` image and its deployment (read-only root, tmpfs, the fixed egress) with the worker's composition root and settings, which logs in as its own role (ADR-0020 log, 2026-10-26). The Browser Use fallback is 8A.6.

**8A.2a — The `BrowserProvider` contract and `ManagedChromiumProvider`** ([ADR-0025](../../adr/0025-browser-and-network-providers.md) §1).
- **The contract** has seven async layers and a capabilities declaration. The layers are Provisioning, Lifecycle, Connection, Execution, Authentication, Networking and Monitoring. The signatures are in [execution-engine.md](../../architecture/execution-engine.md) §3.
- **`ManagedChromiumProvider`**, in the browser-worker package, is the only implementation. It is **the existing code mapped onto the contract, not rewritten**:
  - `SealedProfileProvisioner` and `ProfileWorkspaces` (8A.1);
  - the controller's fenced write-back and `PlaywrightEngine` (8A.2).

  It adds `renew` and `recover`, and runs parallel profiles, each under its own lock.
- **`BrowserController` stays the only entry point.** It holds the lock, the deadline and the record, and calls the provider inside them.
- **Import contracts (AUD-13):** `playwright` and `browser_use` only in `htco_browser_worker`, and the model SDKs only in the gateway package.
- **Async ports (AUD-05):** the new ports are async, and each adapter owns its thread hop.
- **The environment's provider:** `shop_browser_environments.provider` names it (`managed-chromium`). Only the Owner changes it.
- **No other provider** is designed or built: none that attaches to a browser run outside HTCO's browser worker, and no external-vendor adapter. Any future provider must implement the contract and pass the same gates.

| ID | Acceptance criterion |
|---|---|
| AC-8A.2.4 | One shared contract suite runs layer by layer against fixtures and `127.0.0.1` pages only. It covers Provisioning, Lifecycle, Connection, Execution, Authentication, Networking and Monitoring, and it passes for `ManagedChromiumProvider` and for a fake provider. A capability that a provider declares but the suite cannot confirm fails the suite. The controller refuses a recipe that needs an undeclared capability before a session starts. |
| AC-8A.2.5 | CDP runs on a pipe only. Tests show that:<br>- Chromium starts with `--remote-debugging-pipe` and never with `--remote-debugging-port`;<br>- no process in the worker listens on a TCP port for a browser;<br>- with two sessions running, session A, and any page loaded in A, cannot reach or drive session B's browser;<br>- a recipe URL that points at loopback or a private address is refused by the production allowlist. The contract suite's `127.0.0.1` fixture origin is allowed only by a test-only allowlist, which no production configuration can load. |
| AC-8A.2.6 | Only the Owner can change an environment's provider, with `aal2` and step-up, and only to a provider whose readiness is `enabled`. The change is audited. An Admin, an agent and an automation are refused (negative tests). Only `managed-chromium` is `enabled` at the end of this phase. |
| AC-8A.2.7 | import-linter forbids `playwright` and `browser_use` outside `htco_browser_worker`, and the model SDKs outside the gateway package. An architecture test finds no provider class and no Chromium import in the domain, the application layer, the graphs, the execution engine, the gateway, the recipes or the guards. |
| AC-8A.2.8 | Parallel profiles, tested with fixtures. On one worker, sessions for as many different profiles as the declared capacity run at once, each holding its own profile lock. One more session waits for a slot, and the same profile is never open twice. `renew` re-seals refreshed cookies under the lock. `recover` restores the last good archive and runs a health check. **The fence:** session S1's lock connection drops and S2 takes the lock. S1's write-back is then refused, because its lease is checked on its own connection, never by environment. The archive write is fenced too, on that connection or with a generation compare-and-set. Red first, on a fake lock server and on Postgres. |
| AC-8A.2.9 | The provider, configuration, network and dispatch ports are async (AUD-05). A test runs a browser session while the worker's heartbeat task keeps its schedule, with no blocking database call on the event loop. |

**8A.2b — Browser Configuration Profiles** ([ADR-0025](../../adr/0025-browser-and-network-providers.md) §2).
- **One versioned configuration profile per environment.** It has two parts:
  - **The platform baseline, the same for every shop:**
    - the one pinned Chromium;
    - one color scheme;
    - one permissions policy;
    - one viewport, sized for Browser Use and Playwright;
    - standard privacy and security defaults.
  - **Per shop:** the locale and language the shop is really operated in, and the egress assignment.
- **Never configurable:**
  - the User-Agent, the screen resolution or the time zone;
  - geolocation, unless a real, authorized need is recorded in a recipe's scope row, and then only the worker's real location, never a configured coordinate;
  - any fingerprint or "stealth" setting, extensions, and proxy settings outside `NetworkProvider`.
- **The Playwright engine adapter (#122)** gains only the typed options the baseline and the profile allow: locale, viewport, color scheme and permissions.
- **One pinned Chromium for the fleet**, under a controlled update policy.
- **Validation** runs on save and again at session start.

| ID | Acceptance criterion |
|---|---|
| AC-8A.2.10 | Saving a configuration is refused, with each issue named, if it has any of: a never-configurable key, a locale outside the shop's recorded languages, an unallocated dedicated egress, or a permission or download the baseline denies. Nothing is saved, and the same validation at session start refuses the session. Each accepted change is a new version, made by the Owner or an Admin with step-up, and audited. Agents and automations have no tool for it (negative tests). |
| AC-8A.2.11 | In a real Chromium, on a `127.0.0.1` page:<br>- `navigator.userAgent` is the pinned build's own, and the time zone is the worker's;<br>- geolocation is denied, and nothing hides automation;<br>- the viewport and the locale equal the configuration's.<br><br>Two shops' sessions report the same User-Agent, platform, time zone and viewport. They differ only in language, where their languages differ.<br><br>Geolocation:<br>- without a recorded need, it is denied;<br>- with one, the browser reports the worker's real location, and a configuration holding a coordinate is refused.<br><br>The pinned switch list holds the background-traffic switches and Safe Browsing off. A launch that differs from it fails the test. |
| AC-8A.2.12 | Every browser worker runs the one pinned Chromium build, in an image pinned by digest, and a worker whose build differs from the fleet's refuses to start sessions. A profile last opened by a newer build is refused. An update rolls out only after the contract suite and a canary health check pass. Each profile's pre-update archive is kept until the new build has run cleanly for 7 days. That archive is destroyed with the environment and purged with a disconnected shop, and a restore from it takes the session's fence. |
| AC-8A.2.13 | Every session records its configuration version, its Chromium version and its egress ID. Two sessions of one environment that differ in any of them, with no audited change in between, raise a consistency finding. |

**As built (slice 8A.2b, part 1):** migration `20261027130000_browser_configuration_profiles.sql`, pgTAP `39_browser_configuration_profiles`.

- **`htco_domain.browser_configuration`.** The platform baseline (viewport 1280 × 900, a light color scheme, a permissions policy that grants nothing, and the pinned switch list: the five background-traffic switches, Safe Browsing's client-side detection and auto-update off, sync off, crash reports off) and the per-shop settings (`locale`, `egress_id`). `validate_settings` refuses, naming every issue as a code and the setting it concerns, never the value: a never-configurable key (User-Agent, screen, time zone, geolocation, fingerprint, stealth, extensions, proxy, switches, the baseline's own keys and more), an unknown key, a locale outside the shop's recorded languages, and an unallocated dedicated egress. `check_session` runs the same validation again on a stored version and adds what only the start knows: a permission the baseline denies, geolocation without a recorded need, and a worker whose Chromium build differs from the fleet's. Its `LaunchSettings` has no field that could carry a User-Agent, a time zone, a screen, a coordinate or a proxy.
- **The shop's recorded languages** are the `languages` of the shop's Etsy profile, read through the dashboard's cached GET when a version is saved, and kept with the version (`shop_languages`). The session start therefore checks the locale again without a marketplace call, and the database refuses a locale outside that list.
- **`app.browser_configuration_profiles`.** Append-only versions, each exactly the next number for its environment (a trigger, SQLSTATE `40001` for a lost race), bound to the environment, shop and organization by one composite key, and written by a `user` only. There is no column for any never-configurable setting. The Owner and Admins read their organization's versions; no other role, neither worker role nor `service_role`, has any right on it. The browser worker reads its version through 8A.2c's leased-job functions (AC-8A.2.23), not through a grant.
- **`htco_application.browser_configuration`** (async ports, AUD-05). `save_browser_configuration` needs the new capability `edit_browser_configuration` (Owner and Admin, MFA and step-up), checks the edited version (`expected_version`), validates, keeps changing the egress Owner-only (`egress_owner_only`), and writes the version and its audit record (`browser_configuration.saved`, version, locale and egress only) in one transaction. The same settings again write nothing. Agents and automations have no tool or action for it (a registry test).
- **The routes.** `GET` and `PUT /v1/shops/{shop_id}/browser-environment/configuration`. A refusal is `422 browser-configuration-invalid` with `issues: [{code, field}]`; a stale version is `409 browser-configuration-stale` with `current_version`; unreadable languages are `503 shop-languages-unavailable`. Nothing is saved on any of them.
- **The engine.** `PlaywrightEngine.launch(profile, settings=)` passes the baseline's typed options (`args` with the pinned switches, `viewport`, `color_scheme`, `permissions`) and the shop's `locale`, and nothing else; a unit test pins the whole call. CI's `browser` job runs a real Chromium on 127.0.0.1 pages: two shops report the same User-Agent, platform, time zone and viewport and differ only in language (also in `Accept-Language`), `navigator.webdriver` stays true, geolocation is denied, and on Linux the browser's own command line holds the pinned switches.
- **Part 2 (next):** the controller calls `check_session` before a launch and passes the `LaunchSettings` (once #183's provider contract is in); the session records its configuration version, Chromium version and egress ID with the consistency finding (AC-8A.2.13); the fleet build pin, the newer-profile refusal and the pre-update archive (AC-8A.2.12); the real-location check for a recorded geolocation need.

**8A.2c — Browser dispatch and resume** ([ADR-0025](../../adr/0025-browser-and-network-providers.md) §3, amending ADR-0019; audit AUD-04). This slice lands before any recipe runs through the gateway (8A.5, 8A.10).
- **The Browser channel in the agent-worker dispatches.** It returns `DISPATCHED` with one `browser.run` job, and holds no lease, slot or connection while the run goes on.
- **The browser worker re-checks before it acts.** It claims the job and re-checks the claim, the deadline, the status, the flag, the scope and every kill switch. For every approval-gated run (every Etsy browser action, reads included, D-16) it also re-checks the approval, read-only, inside `start_browser_run`, before any page loads. Then it runs the recipe through the controller and publishes one outcome.
- **Revoking an approval also cancels the dispatched action**, in the same transaction.
- **A durable started marker** is committed before any approval-gated run (every Etsy browser action) or write touches a page.
- **A resume re-enters `submit` with the stored claim**, as the human hand-off already does.
- **A write is never re-sent blindly.** A write that started but has no outcome goes to a person for reconciliation.

| ID | Acceptance criterion |
|---|---|
| AC-8A.2.14 | A Browser binding returns `DISPATCHED`. In the same transaction, three things are written: one `browser.run` job keyed by execution and attempt, one deadline watchdog (`browser.resume` at the deadline plus grace), and `actions.dispatched.v1`. While the run goes on, the automation execution waits without its job lease, the gateway's slot is released, and no connection is held (tested with a fake browser worker). The job's payload is a locator only: the execution ID and the attempt. It carries no recipe, scope, environment, parameter, secret, token or authority (schema test). The browser worker runs only the binding stored with the claim: a job edited to name another recipe or parameters changes nothing that runs (tested). |
| AC-8A.2.15 | Before it touches a page, the browser worker re-checks:<br>- the claim is still `DISPATCHED`, with the same request hash;<br>- the deadline has not passed;<br>- the environment is `READY`;<br>- the flag is on and the scope row is recorded;<br>- no kill switch is on;<br>- whenever the action needs an approval (every Etsy browser action, reads included, D-16), that approval is still `APPROVED` for this request hash and not expired.<br><br>Each failed check ends the run `refused` with its code, and no page loads. A job the caller has not leased, or one for an execution that is not `DISPATCHED`, is refused.<br><br>An approval cancelled or expired after dispatch, before the attempt has started, for a read or a write:<br>- moves the `DISPATCHED` action to `CANCELLED` in the same transaction;<br>- even when that transition is withheld in the test, the start function's own check refuses the run with `approval_revoked`.<br><br>No page loads either way.<br><br>Cancellation and start are serialized on the action execution's row. A revocation that lands after the attempt's `started` row is committed does not cancel the action. Its outcome is still processed, the resume records `HANDED_TO_HUMAN`, and one Owner task says that the approval was revoked while the write ran. Concurrency test: a revocation racing the start ends in exactly one of the two paths. |
| AC-8A.2.16 | The outcome is published once per execution and attempt: a row, and `browser.run_finished.v1`. The outbox dispatcher's delivery of that event enqueues one `browser.resume`, idempotent per execution and attempt. The resume resubmits the same request. `submit` resumes the stored claim, validates the output against the action's output schema, verifies it, and records exactly one final result. It never runs the browser step again, and replaying the event changes nothing. |
| AC-8A.2.17 | A read run that dies is retried within its policy. A challenge ends the run as `challenge`, and the resume records `HANDED_TO_HUMAN` with one Human task.<br><br>**The deadline watchdog** fires at the deadline plus grace when no outcome has arrived, including for a `browser.run` that was never claimed or was dead-lettered. The grace is at least the session's maximum duration, and the watchdog is tested with no browser worker running at all:<br>- an attempt with no `started` row fails as `timeout`, and a late start of it is refused;<br>- an attempt with a `started` row and no final status is set to `interrupted` and goes to a person, never re-sent.<br><br>**Only the watchdog sets `interrupted`**, after the deadline plus grace and under the profile lock. Against a live session that overruns, it reschedules itself and does not mark the run. After its capped reschedules, it opens an incident and alerts the Owner without marking the run (tested). No execution waits for ever unseen.<br><br>**The partition case.** The browser worker's database connection is cut while the site stays reachable, so its lock is freed and its job is redelivered. The redelivered job finds the `started` row and exits, touching neither the page nor the row. Nothing is marked before the first session's deadline plus grace (tested). |
| AC-8A.2.18 | pgTAP, in its own organization. The `htco_browser` role can:<br>- claim only `browser.*` jobs;<br>- start an attempt only through `private.start_browser_run(job_id, lease_token)`;<br>- set a final status once only through `private.finish_browser_run(run_token, outcome)`, and insert an unapproved read's row through it with its leased job.<br><br>It cannot write or lock `action_executions` itself, and it cannot write approvals or another organization's rows. `authenticated` has no access to run rows.<br><br>Both functions (`security definer`):<br>- take the execution, attempt and organization **from the `browser.run` job row leased with the given token**, never from their arguments or from `htco.organization_id`;<br>- refuse a job that is not leased by that token, or whose lease expired, and an execution whose organization differs from the job's;<br>- set `search_path = ''` and schema-qualify every name;<br>- can be executed by `htco_browser` only, with `EXECUTE` revoked from `PUBLIC`.<br><br>pgTAP covers each of these. One case sets `htco.organization_id` to another organization, and the call still acts only on the leased job's own execution. |
| AC-8A.2.19 | **The started marker.** Before an approval-gated run (every Etsy browser action) or any write touches a page, the browser worker starts the attempt through `private.start_browser_run`. In one transaction, the function locks the action execution's row, re-checks it, and commits the attempt's run row with the status `started` and `started_at`. The insert is `on conflict do nothing`, so an attempt can be started only once.<br><br>A job redelivered for an attempt that has a `started` row and no final status never runs the write again. It exits, touching neither the page nor the row. Only the deadline watchdog later sets `interrupted`, and the resume then records `HANDED_TO_HUMAN` with one reconciliation task.<br><br>A write whose job died before its marker never touched a page, and may be retried within its policy.<br><br>Tested twice, red first: by killing the worker between the marker and the outcome, and by redelivering the job through `release_expired`. |
| AC-8A.2.20 | **No private-network access, after DNS resolution.** In the production network profile:<br>- a fixture name that first resolves to a public address and then to `127.0.0.1` (DNS rebinding) cannot reach the loopback service;<br>- a public fixture page that redirects to `http://127.0.0.1:…`, `http://[::1]/` or `http://169.254.169.254/` reaches none of them;<br>- a subresource or a WebSocket aimed at a private or link-local address is dropped.<br><br>Each case is refused by the controller where the host is an IP literal, and dropped by Chromium's user's egress rules otherwise. The browser worker's own process still reaches its exact internal endpoints. |
| AC-8A.2.21 | **Only the starter gets the binding, and every run can record its outcome.**<br>- `start_browser_run` returns the request, the binding and a run token only to the caller whose `insert … on conflict do nothing returning` created the `started` row. A redelivered worker for the same attempt gets nothing, not even the binding, and touches no page (tested by redelivering with `release_expired`).<br>- `finish_browser_run(run_token, outcome)` records the outcome of a run whose queue lease has expired, sets it once, and refuses a token of another attempt, a used one, or a forged one (pgTAP).<br>- While a run goes on, its job's lease is renewed on a heartbeat. A run longer than one lease period is not redelivered while the heartbeat beats (tested), and a stopped heartbeat makes it redeliverable without giving the redelivered job the binding. |
| AC-8A.2.22 | **The egress rules fail closed.** At startup, and before each session, the browser worker proves that the per-user egress rules for Chromium's OS user are active:<br>- probes from that user to `127.0.0.1`, `[::1]`, `169.254.169.254` and one address in each private range are dropped;<br>- the worker's own process still reaches its exact internal endpoints.<br><br>If any probe connects, or the rules cannot be read or applied (for example where the platform grants no `NET_ADMIN`), the worker starts no session. It reports `egress_rules_missing` in the health view and alerts the Owner. A test runs the worker with the rules removed and shows that no session starts. |
| AC-8A.2.23 | **Profile archives only through a leased job.** `htco_browser` has no direct grant on `app.browser_profile_archives`. Opening a profile and the fenced write-back go only through `security definer` functions bound to a `browser.*` job leased with the caller's token, as `start_browser_run` is. That covers `renew`, `recover`, provisioning and destroy, which are all browser jobs. The functions:<br>- take the environment and organization from the leased job row, never from an argument or `htco.organization_id`;<br>- write with the generation compare-and-set of ADR-0025 §1, so the fence still holds;<br>- set `search_path = ''`, and can be executed by `htco_browser` only.<br><br>pgTAP: with the setting pointed at another organization, or with an unleased or expired job, no archive can be read or written. A leased job reaches only its own environment's archive. |
| AC-8A.2.24 | **Implementation details from #136's review** (sage-arch):<br>- **Two explicit functions finish a run** (sage-arch, and local Codex P1 on #136 at 4096175):<br>  - `finish_browser_run(run_token, outcome)` for a started run, which works after its job lease has expired;<br>  - `record_unapproved_read(job_id, lease_token, outcome)` for a read that needs no approval. It validates the lease, derives the execution and organization from the job, and inserts the final row once, atomically.<br><br>  The lease-expiry refusal applies only to the start and to that insert. `browser_run_outcomes` stores `run_token_hash`.<br>- **The 8A.6 minimal gateway is the 9.1 package itself**, not a copy, and its interim ledger is the table that 9.4 extends.<br>- **The egress self-test (AC-8A.2.22) runs on the real platform** that hosts the browser worker: the droplet of D-18 (a), or App Platform if it ever grants the rules. |
| AC-8A.2.25 | **One live session per profile, even under a partition** (local Codex P1 on #136 at 4096175). `start_browser_run` locks the environment's row and refuses any start while a `started` run without a final status exists for that profile. The gate clears only with that run's final outcome or the watchdog's reconciliation. While a session works, the controller watches its lock lease. If the lease is lost, it kills Chromium's process group at once and cuts that session's egress, and the session ends `lock_lost`. Tested with `pg_terminate_backend` during a run: a second execution for the same environment cannot start until the first resolves. |
| AC-8A.2.26 | **No check-then-start gap for the gates** (local Codex P1 on #136 at 4096175). `start_browser_run` re-reads every mutable gate itself, under the same transaction as the started marker:<br>- the environment's status (`READY`);<br>- the action's flag and its scope row;<br>- the automation's readiness;<br>- every kill switch.<br><br>A gate change takes the matching row lock, or bumps a generation counter that the start compares, so a change committed between the worker's own check and the start is always seen. Concurrency test: switching a kill switch or a flag off while a start races refuses the start or lets it run, never both, and a run started after the change is refused. |
| AC-8A.2.27 | **Chromium resolves names only through the pinned resolver** (local Codex P2 on #136 at 4096175). Chromium's DNS is DNS over HTTPS to the resolver chosen in ADR-0026 §6, written as an IP literal in the policy's template, so no system resolver is needed to bootstrap it. Chromium's OS user has no plain DNS port and no system resolver: `127.0.0.11`, `127.0.0.53` and private resolvers stay blocked by the egress rules. Tested in the production network profile: public names resolve and load, HTTP, WebSocket and other traffic to private addresses stays blocked, and the rebinding test of AC-8A.2.20 still passes. |
| AC-8A.2.28 | **The profile lock comes before the started marker** (hosted Codex P2 on #136 at 88f44b4). The order is:<br>1. The browser worker takes the profile lock and its fence first.<br>2. It then calls `start_browser_run` **on the lock's own dedicated connection**. The function refuses unless that connection's backend holds the environment's profile lock (`pg_locks` for its own `pg_backend_pid()`). The marker is therefore always committed under the lock.<br>3. The worker keeps the lock through the run and the outcome publication.<br><br>A worker paused between taking the lock and the start cannot be overtaken: no one else can start that profile or mark it while the lock is held. A worker that resumes after its deadline is refused by the start function's deadline check and by the controller's check before launch.<br><br>Tests:<br>- a start attempted without the lock is refused;<br>- a paused worker resumed after the watchdog has run cannot touch the page;<br>- the lock is held from before the marker until the outcome is published. |

### 8A.3 — Preflight, session validation and network health

- **Preflight:** Chromium starts, the disk has space, DNS and TLS to the target work, the egress IP equals the configured one, and a JavaScript and resource probe page loads.
- **Session validation:** the signed-in marker for the site.
- **Network diagnostics (daily):** egress stability, latency and a reputation lookup of our own egress IP. These are recorded as diagnostics only and never used to switch IPs.
- **Status updates:** `READY` ↔ `DEGRADED` ↔ `LOGIN_REQUIRED`.
- **The `NetworkProvider` port** ([ADR-0025](../../adr/0025-browser-and-network-providers.md) §4), async: `egress_for(shop)`, `allocate` and `release` (owner-only, optional) and `check`. It supplies the egress that each configuration profile names (8A.2b).
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
| AC-8A.3.7 | Allocating, changing or releasing a shop's egress is Owner-only (`aal2`, step-up) and audited. Agents and automations have no tool for it (negative tests). A dependency and configuration test finds no third-party proxy-service client or commercial VPN client in the browser worker. A dedicated egress is HTCO's own gateway on HTCO's own droplet. |

### 8A.4 — Security challenge handler and human handoff

- **Detectors:** CAPTCHA widgets, passkey and WebAuthn prompts, "verify it's you" pages, block or "unusual activity" pages, and permission errors. Each has recorded fixtures.
- **On detection:** stop, record a screenshot **hash** (the image itself is kept 7 days for the reviewer only), open a Human task with a link, and move the environment to `LOGIN_REQUIRED` or `DEGRADED`. Two challenges in 24 hours move it to `SUSPENDED`.
- **Manual login:** the person signs in through Live View and Takeover of the same session (slice 8A.11, [ADR-0026](../../adr/0026-browser-live-view-and-human-takeover.md)): a CDP screencast and typed input, never noVNC. Then the engine resumes from its checkpoint.
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

### 8A.6 — Browser Use driver for employees (moved earlier, ADR-0025 §5)

- [Browser Use](https://github.com/browser-use/browser-use) (MIT) is wrapped behind `BrowserSession`. It is the **primary AI browser engine** for employees, driving the provider's pipe connection in process, never a TCP or WebSocket CDP endpoint (ADR-0025 §5). A Browser Use version that needs a CDP URL is not adopted.
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
  - **PII masking.** DOM and accessibility text is masked for DC1. No screenshot of a signed-in page goes to a model (ADR-0025 §5).
  - **Recording and kill switches.** Every call is recorded. Kill switches act globally, per shop, per employee and per alias, plus the provider kill switch.

| ID | Acceptance criterion |
|---|---|
| AC-8A.6.1 | Every Browser Use model call appears in `routing_decisions` and `model_usage` with the employee and task. |
| AC-8A.6.2 | An automation actor requesting a non-deterministic binding is refused (repeats AC-5.13.2 end to end). |
| AC-8A.6.3 | Browser Use's model client calls only the `ModelGateway` port. An architecture test finds no provider SDK and no provider API host in the browser-worker package, and the browser worker's egress allowlist refuses provider API hosts (test). |
| AC-8A.6.4 | The browser worker holds no provider key. Its per-run gateway token is bound to the execution, employee, shop and alias, and it expires with the run's deadline. The gateway refuses a token that is expired, reused for another run or replayed, or that names another alias (negative tests). |
| AC-8A.6.5 | Every call uses the `browser-agent-default` alias. With that alias's model unhealthy, its circuit open or its kill switch on, the run stops before any provider request and is handed to a person. It is never re-routed (as AC-9.3.8). |
| AC-8A.6.6 | A call whose reservation would pass the run's cost, token or step cap, or the shop's daily cap, is refused before any provider request, and the refusal is recorded. Settlement replaces each reservation, and a failed call releases its reservation. |
| AC-8A.6.7 | On a fixture order page with fake buyer data, the text sent to the fake provider has the DC1 values masked. No screenshot of a signed-in page is ever sent, whatever the recipe declares. A fixture where a recipe that declares no buyer data meets an unexpected buyer-data page (a redirect, a notification) sends no screenshot and masks the text. |
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
- **Admin › Browser environments**, the central management screen ([ADR-0025](../../adr/0025-browser-and-network-providers.md) §2). It lists every shop environment the viewer may see, with:
  - its status and configuration version;
  - its Chromium version and egress;
  - its last health check and last session.

  It offers Create (step-up and consent), Edit configuration (validated before saving), Check now, Provision, Re-provision and Recover. Egress and provider changes stay Owner-only. Web first (ADR-0016).

| ID | Acceptance criterion |
|---|---|
| AC-8A.9.1 | Creating an environment requires step-up and the consent text; both are recorded. |
| AC-8A.9.2 | The kill switch stops running sessions within one step and refuses new ones. |
| AC-8A.9.3 | The management screen lists only the environments that the viewer's role and shop access allow (RLS). It names each validation issue before saving. Every change needs step-up, and changes to the egress or the provider are Owner-only (negative tests). |
| AC-8A.9.4 | Check now, Provision, Re-provision and Recover each enqueue a browser job; the API never runs a browser. The screen shows each result once its outcome is published. |

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

### 8A.11 — Live View and Human Takeover ([ADR-0026](../../adr/0026-browser-live-view-and-human-takeover.md))

**It starts with a spike.** On the fleet's pinned build in new-headless mode, over the pipe, against a `127.0.0.1` page, it confirms that:
- `Page.startScreencast` delivers frames;
- `Input.dispatchMouseEvent`, `Input.dispatchKeyEvent` and `Input.insertText` reach the page;
- the managed policies of ADR-0026 §6 apply to this build;
- WebSockets work end to end: the dashboard's socket through the API's production edge, and the browser worker's private dial-out to the relay from its own host.

The result goes in ADR-0026's validation log before the rest is built.

- **Live View.**
  - The session's CDP screencast is relayed by the browser worker, which dials out to the API's internal relay, to an authenticated WebSocket on the dashboard's origin.
  - Each viewer needs a single-use ticket bound to the user, organization, shop and session, valid for 60 seconds, plus an `Origin` check.
  - Viewers see the facts as HTCO's own labels: status, actor, shop, step, the last action's type, and each tab's normalized route. A page's title and raw path go only to buyer-data viewers, and are never stored.
  - Frames are held in memory only. They are never stored and never sent to a model.
- **Takeover.**
  - `VIEW_BROWSER_SESSION` and `CONTROL_BROWSER_SESSION` are separate permissions.
  - One Controller at a time holds a lease (`browser_control_leases`) with a timeout, renewal by its holder only, and a maximum duration. The Owner may take control for any purpose, and an Admin only for sign-in, MFA, a challenge or a stuck step (D-25). Every takeover is limited to its purpose's path scope, with the W3, money and account pages always blocked.
  - The run pauses at a safe point. The session's `control_state` becomes `HUMAN_CONTROLLING`, and the action stays `DISPATCHED`, reported as `waiting_for_human_control`. The worker, the browser, the profile lock, the run token and the job's lease stay held.
  - A takeover never writes. Every request that could change the shop is refused, apart from the sign-in, MFA and challenge flows' own, and the write is the run's own Gateway command after the return.
  - A single input gate takes a closed set of typed events.
  - Return Control re-checks everything, then resumes the same attempt from its checkpoint.
- **The privacy pack** (ADR-0026 §6). One pinned managed-policy file covers:
  - DNS over HTTPS with HTCO's chosen resolver;
  - no extensions;
  - third-party cookies and the Privacy Sandbox ad features off, plus a pinned tracker blocklist;
  - no sync, no metrics, no crash reports;
  - no helpers that send data out.

  The worker checks that the file is in effect at start (fail closed). Per-session cleanup leaves only the allowlisted origins' cookies and storage.
- **The screen** (`htco-clients`, web first): Live View, Take Control, Return Control, and the holder's name.

| ID | Acceptance criterion |
|---|---|
| AC-8A.11.1 | **An authorized view works.** An Owner or Admin, or Operations and Support on a granted shop, opens a session's Live View with a fresh ticket. Frames and facts arrive, and a dropped connection reconnects with a new ticket from the latest frame. Each of these is refused (negative tests):<br>- a viewer without `VIEW_BROWSER_SESSION`, from another organization, or without the shop;<br>- a reused or expired ticket;<br>- a cross-origin upgrade.<br><br>No frame is ever written to the database, a disk or a log, and none is sent to a model. |
| AC-8A.11.2 | **Redaction fails closed.**<br>- Frames of any signed-in page go only to viewers with `VIEW_BUYER_CONTACT` on that shop, whatever the recipe declares. Everyone else gets the facts and a placeholder. That includes a fixture where a recipe that declares no buyer data meets an unexpected buyer-data page.<br>- While a secret is typed, no frame is captured, and the stream stays paused until a navigation or a verified mask (AC-8A.11.20). |
| AC-8A.11.3 | **One Controller.**<br>- Two concurrent Take Control requests yield exactly one lease, and the second is refused with the holder named.<br>- Only the holder renews. A lease without renewal expires, and the maximum duration ends it.<br>- Taking control needs `CONTROL_BROWSER_SESSION`, `aal2` and a step-up.<br>- Agents and automations have no tool for viewing or control (negative tests). |
| AC-8A.11.4 | **No conflict with the agent.**<br>- Take Control pauses a deterministic recipe between steps, and a Browser Use or LangGraph run between agent steps.<br>- While the session is `HUMAN_CONTROLLING`, every agent or automation action is refused at the input gate, and while the agent runs, every Controller event is refused (both directions tested).<br>- The profile lock, the browser, the run token and the job's lease stay held, and the session's deadline is suspended up to the lease's maximum. |
| AC-8A.11.5 | **Typing, clicks and scroll work** in new headless, against a `127.0.0.1` fixture page. Key events, `insertText`, mouse presses and wheel events each change the page's DOM as expected.<br><br>Refused:<br>- an event outside the closed set, or one from anyone but the lease holder;<br>- an event over the rate or size limits;<br>- a raw CDP command.<br><br>A navigation outside the allowlist, a payment page, or a private address (AC-8A.2.20) is blocked during a takeover too. |
| AC-8A.11.6 | **Manual MFA in the page.**<br>1. A fixture sign-in with a TOTP code page, a challenge drawn in the page's own DOM, hands the run to a person.<br>2. The Controller takes over, completes the MFA, and returns control.<br>3. The profile is re-sealed under the fence, and the next session is signed in.<br><br>The typed code appears in no log, audit record, model input or frame.<br><br>**Native WebAuthn and passkey prompts are out of scope** (ADR-0026 §2). A fixture that raises one stops the session as ADR-0020 rules, with `LOGIN_REQUIRED` and a Human task. No takeover claims to complete it. |
| AC-8A.11.7 | **Return Control re-checks.** Before anything resumes, it checks the page state, the run's permissions, the flags and the scope, every kill switch, the approval of every approval-gated run (D-16), and the deadline. A failed check ends the run as `HANDED_TO_HUMAN` or `FAILED` and never resumes it. A deterministic recipe resumes from its checkpointed step, and a LangGraph run from its interrupt, with the current page as its observation. |
| AC-8A.11.8 | **No double execution.**<br>- The same attempt resumes after a return, keeping its run token, and no new attempt starts.<br>- A write step that was in progress, or is uncertain, at the takeover is not re-run unless a re-read shows that it did not happen, and otherwise goes to a person.<br>- The person cannot write (AC-8A.11.18). A change the run did not make, found by verification, opens an incident, and the run does not write.<br>- The gateway records exactly one final result. |
| AC-8A.11.9 | **Disconnects and crashes.**<br>- When the Controller's connection drops, the lease expires and the run stays paused; it never returns to the agent by itself. At the maximum duration the run becomes `HANDED_TO_HUMAN`, and the session closes with a fenced re-seal.<br>- When the browser worker is killed during a takeover, Postgres releases the profile lock, the last good archive stays, and the attempt resolves through the started marker and the watchdog with no double write. Viewers see "session ended".<br>- When the relay is killed, the outcome is the same as a disconnect. |
| AC-8A.11.10 | **Every takeover is audited:** the request with its purpose, the grant, each renewal, the release or expiry, each navigation (as its normalized route), each refused request (its normalized route, method and count), the number of input events, and the result of the return checks. Viewing start and end are audited too. No key or text value, page title, raw path or query string appears anywhere. |
| AC-8A.11.11 | **The privacy pack is in force.** The managed policy file is pinned in the image, and the effective policies equal it. A missing or changed policy starts no session (`policies_missing`). DNS goes only to the chosen DoH resolver, with no plain-DNS fallback. Extension installs are blocked. Sync, metrics and crash reports are off. After a session, the re-sealed archive holds only the allowlisted origins' cookies and storage. |
| AC-8A.11.12 | **The headless spike is recorded** in ADR-0026's validation log before 8A.11 is built further: screencast and input over the pipe in new headless, on the pinned build, and WebSockets end to end (the dashboard's socket through the production edge, and the worker's private dial-out to the relay from its host; sage-arch on #166). If frames don't arrive, ADR-0026 is amended (headed Chromium on Xvfb in the same worker) before any code relies on it. |
| AC-8A.11.13 | **The takeover scope is deny by default.**<br>- **Sign-in, MFA and challenge:** a takeover reaches only the environment's recorded sign-in, verification and challenge paths.<br>- **Stuck step:** a takeover reaches only its run's recipe allowlist.<br>- **Anything else is blocked and shown as blocked.**<br><br>Negative tests, on fixture pages, for every Controller, the Owner included: the refund, coupon, Ads, delete-listing, payments, payouts and bank, billing, password and password-reset, two-factor, connected-apps and shop-closure pages are each refused. |
| AC-8A.11.14 | **Who controls** (D-25, decided: Admins limited).<br>- **The Owner** may take control for any purpose.<br>- **An Admin** may take control only for sign-in, MFA, a challenge or a stuck step, with no separate grant.<br>- Every takeover reaches only its purpose's pre-approved path scope (AC-8A.11.13). The blocked pages stay blocked for the Owner too.<br><br>Negative tests:<br>- an Admin asking for any other purpose is refused;<br>- an Admin in an allowed purpose who navigates to a blocked page (refunds, coupons, Ads, a deletion, money or account pages) is refused, and so is the Owner;<br>- Operations, Support and every other role cannot take control at all. |
| AC-8A.11.15 | **Implementation details from #166's review** (sage-arch):<br>- **Long-lived sockets re-check authorization.** At each renewal, and at least every minute, the viewer's and the Controller's role, shop access and grant are re-checked. A revoked right or an ended session closes the socket at once.<br>- **No frame while the person types a secret.** When focus enters a password or one-time-code field, frames pause (the same rule as the Secret Broker's typing).<br>- **The watchdog respects the takeover.** While a session is `HUMAN_CONTROLLING`, its deadline is suspended up to the lease's maximum, and the watchdog's deadline moves with it. Taking control records that bounded extension on the run's stored claim, in the lease's transaction (quill on #166). While a control lease is valid, the watchdog never marks the run `interrupted` and never raises an incident.<br>- **The DoH resolver keeps no query logs**, by its published policy, and its choice is recorded in TOOLCHAIN. |
| AC-8A.11.16 | **The path scope holds in a single-page app and in new pages** (sage-arch and quill on #166). Etsy's Shop Manager is partly a single-page app:<br>- the controller watches `Page.navigatedWithinDocument` and stops the takeover on a `pushState` into a blocked or out-of-scope path;<br>- the API endpoints behind the blocked pages are blocked too, for every request type (XHR, fetch, WebSocket, form posts), not only for navigations;<br>- the path scope, the write block, the payment block and the screencast cover every new page in the context: a `target=_blank` link, a `window.open` popup and any other. A page they cannot cover is closed.<br><br>Tested with an SPA fixture that reaches a blocked view by `pushState` and by a background API call, and with a fixture that opens a blocked page and a payment page in a popup and in a new tab. |
| AC-8A.11.17 | **Narrow scopes** (sage-arch on #166, with D-25):<br>- **A stuck step** reaches only the current step's URL patterns, wherever the recipe declares pages per step, and otherwise the recipe's allowlist. Tested: a page of a later step is refused.<br>- **The Owner's "other" purpose** has an explicit deny-by-default scope: the shop's recorded Shop Manager paths, minus every blocked page. Tested: a path outside that list is refused, as is a blocked page inside Shop Manager.<br>- **Edits made by hand cannot happen.** The write block (AC-8A.11.18) refuses them, in place of flagging them afterwards. |
| AC-8A.11.18 | **A takeover never writes** (Codex on #166). Every change to a shop goes through the Action Gateway.<br>- During a takeover, the context's request interception refuses every method other than `GET` and `HEAD`, every form submission and every WebSocket message to the site. The only exceptions are the recorded sign-in, verification and challenge endpoints, in those purposes only, and the endpoints recorded as read-only.<br>- Negative tests on fixture pages, for every Controller, the Owner included, in every purpose: a form submit, a `fetch` or XHR `POST`, `PUT`, `PATCH` or `DELETE`, a WebSocket message and a background autosave are each refused and never reach the fixture server, from the page, a popup or a new tab. The Controller sees "this change goes through HTCO".<br>- A stuck write step: the person fixes a field and returns control, and the run re-applies its command's values and submits once, under its approval and key. If the person changed a field that the command does not set, the run does not submit and goes to `HANDED_TO_HUMAN`.<br>- A fixture change that the run did not make, planted behind the block, opens an incident at verification, and the run does not write. |
| AC-8A.11.19 | **No buyer data in facts or audits** (Codex on #166).<br>- Viewers without `VIEW_BUYER_CONTACT` get only HTCO's labels and normalized routes, never a page title, a raw path, a query string or a fragment. Tested with a fixture whose title and path hold a buyer's name, username, order ID and email.<br>- A URL that matches no recorded pattern shows only its site host and "unrecognized page".<br>- None of the four values appears in an audit row, a log line or an event. Viewers with `VIEW_BUYER_CONTACT` get the title and raw path live only. |
| AC-8A.11.20 | **The secret pause holds** (Codex on #166).<br>- The input gate pauses the stream before it dispatches the first key or text event into a secret field, and the session pauses it before it types a secret.<br>- At the pause, the relay drops its cached frame and every frame in flight. A viewer who reconnects during the pause gets no frame.<br>- The stream never resumes on blur. A fixture code field that stays visibly filled after blur keeps the stream paused until a navigation or until the field is cleared, and no frame ever shows its digits.<br>- A custom code widget on a recorded verification page, with no type or `autocomplete` hint, pauses the stream too. |
| AC-8A.11.21 | **One status vocabulary** (quill and sage-arch on #166).<br>- A takeover changes one record, the browser session's `control_state`, and only along the transition table in execution-engine.md §7. The database refuses any other transition.<br>- The action stays `DISPATCHED` throughout, and the status layer reports the waiting reason `waiting_for_human_control` in place of `waiting_for_browser`. No new action, task or automation state exists for a takeover. |

## Test plan

- Unit tests for the controller, router and guards with fakes.
- Recorded-page fixtures for challenges and injections, served locally.
- Integration tests with a real Chromium against a local test site (never etsy.com in CI).
- pgTAP for the roles.
- A manual verification on the owner's shop with B-02 only, after its scope row and flag.
- The shared `BrowserProvider` contract suite, layer by layer, against `ManagedChromiumProvider` and a fake provider. It includes the honesty checks of AC-8A.2.11 in a real Chromium.
- Configuration validation: every never-configurable key, every conflict, at save and at session start.
- Dispatch and resume with a fake browser worker:
  - lease and slot release;
  - the execution-time re-checks;
  - a forged or replayed job;
  - outcome replay;
  - a lost read and a lost write.
- Egress consistency tests with a local echo endpoint.
- Browser Use against a fake `ModelGateway` that records every call: caps, masking, kill switches and the stop on an unavailable alias.
- Tests for the per-run gateway token: expired, replayed, wrong alias.

## Security checklist

- [ ] No stealth plug-ins, fingerprint changes, proxies or CAPTCHA services in the dependency tree (dependency allowlist test).
- [ ] The browser worker holds no OAuth tokens; profiles are sealed; secrets only through grants.
- [ ] THREAT_MODEL updated (browser environment assets, profile theft, injection, session hijack).
- [ ] The browser worker holds no provider key. Browser Use's model calls go only through the Model Gateway, with a per-run token, the pinned alias, hard caps, masking, recording and kill switches.
- [ ] Only `managed-chromium` is an enabled provider. No provider attaches to a browser run outside HTCO's browser worker, and there is no external-vendor adapter. Any future provider must implement the contract and pass the same gates (ADR-0025 §1).
- [ ] Business logic never imports Chromium, Playwright, Browser Use or a provider class, and the model SDKs stay in the gateway package (import contracts, AUD-13).
- [ ] The browser stays honest (ADR-0025 §2):
  - the real User-Agent and the worker's time zone;
  - one viewport and one pinned Chromium for every shop;
  - no geolocation without a recorded, authorized need;
  - no fingerprint, stealth or proxy setting.

  A configuration that breaks this is refused at save and at session start.
- [ ] Browser work is dispatched (ADR-0025 §3):
  - the job carries no secret, token or authority;
  - the browser worker re-checks the claim, the flag, the scope and every kill switch before it acts;
  - for every Etsy browser action, reads included (D-16), the approval is re-checked read-only inside `start_browser_run` before any page loads, and revoking it cancels the dispatched action in the same transaction (`approval_revoked`);
  - a durable started marker is committed before any approval-gated run or write touches a page;
  - its role cannot write action executions or approvals;
  - a write is never re-sent blindly: one that started without an outcome goes to a person.
- [ ] Egress is truthful: shared, or a dedicated HTCO-owned static IP, never rotated and never hiding shop linkage. Changes are Owner-only and audited, and consistency is checked before every session.
- [ ] CDP runs on a pipe only. There is no TCP port, and no session or page can reach another session's browser (AC-8A.2.5).
- [ ] No browser request reaches loopback, private, link-local or multicast addresses. This is enforced after DNS resolution by Chromium's user's egress rules, on IPv4 and IPv6, for redirects, subresources and WebSockets (AC-8A.2.20).
- [ ] Browser privacy defaults are pinned: the background-traffic switches are on and Safe Browsing is off (AC-8A.2.11).

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
| A future provider weakens the honest design | Only `managed-chromium` is enabled. Any future provider must implement the contract and pass the same gates: the shared contract suite, the SECURITY GATE checklist and ADR-0020's rules ([ADR-0025](../../adr/0025-browser-and-network-providers.md) §1) |
| A shop's egress or configuration changes unnoticed | A consistency check before every session. Each session records its egress ID, configuration version and Chromium version. Changes are owner or admin actions, audited (AC-8A.2.13, AC-8A.3.4 to AC-8A.3.7) |
| A Chromium update breaks sessions for every shop at once | The contract suite and a canary health check before the fleet moves; each profile's pre-update archive kept for 7 days; no downgrade of a newer profile (AC-8A.2.12) |
| A browser run dies mid-write and its effect is unknown | It is never re-sent. Past its deadline it becomes `HANDED_TO_HUMAN` with a task to check (AC-8A.2.17) |

## Cost impact

See [COST_MODEL §6](../../cost/COST_MODEL.md#6-architecture-upgrade-2026-10-23-execution-browser-authenticator):

- `browser-worker` on a basic HTCO droplet of 2 GB, about $12/month. App Platform cannot apply the egress rules (AC-8A.2.22);
- the shared browser egress is that droplet's own IP, at no extra cost (D-18 (a)). An optional per-shop egress is about $5/month per shop when enabled;
- Grafana OSS self-hosted $0, or Grafana Cloud free tier;
- Browser Use model calls from the employee budgets.
