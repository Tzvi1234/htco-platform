# Architecture and maintainability audit, 2026-10-23

This audit read all the code and the plan on `main` at `c9dd4ad`, after slice 5.4 (#72). It checked them against these sources:

- [ENGINEERING_RULES](../engineering/ENGINEERING_RULES.md), [TESTING_STRATEGY](../engineering/TESTING_STRATEGY.md) and [SOFTWARE_DESIGN](../architecture/SOFTWARE_DESIGN.md);
- the import-linter contracts in `pyproject.toml`;
- the ADRs, [execution-engine.md](../architecture/execution-engine.md), [PLAN](../plan/PLAN.md), the phase files and [EXECUTION_ORDER](../plan/EXECUTION_ORDER.md).

The clients have their own report, `htco-clients/docs/reviews/2026-10-23-architecture-audit.md`.

**Severity** measures the risk to maintainability:

- **P0:** this will create a parallel subsystem or a contradiction that is expensive to undo if it is not settled before the next slice.
- **P1:** a real defect in layering, privacy or consistency, or a document that misleads the next person.
- **P2:** contained duplication or a gap in a rule.
- **P3:** hygiene.

In-flight areas were reported only and not changed:

- 5.5 reconciliation (#75), 5.8, 5.9 and 5.11 belong to `p5-pipeline`.
- Phase 6 belongs to `phase6`.

## What holds

These points were verified:

- **Domain and application are pure.** `htco_domain` imports only the standard library. `htco_application` imports only the standard library and the domain: no httpx, pydantic, structlog or anyio (`tests/architecture/test_layer_purity.py`). Neither calls `datetime.now`; the clock is injected.
- **API routes are thin.** Routes parse the request, call one use case and map the result. Examples are `shop_data.py:429-740` (pure mapping), `automations.py:325-399` and `etsy.py`. No router imports persistence or integrations; only `main.py` and `cli.py` do.
- **Ports are application Protocols.** The only Protocols in adapter packages are infrastructure ones: `htco_platform` ErrorReporter, JwksSource and ReadinessCheck.
- **LangGraph is confined.** Nothing imports it yet, and the contract exists.
- **Etsy is GET-only.** `integrations/etsy.py:95` defines `READ_ONLY_METHODS = {"GET"}`, and all 17 `_request` calls pass GET. A second guard is the httpx hook at `etsy.py:762-781`. The only POST goes to the OAuth token endpoint (`etsy_oauth.py:100`).
- **Clean areas:**
  - No TODO, FIXME or XXX anywhere.
  - No skip, xfail or skipif in `tests/`.
  - No f-string log calls; logs carry error type names.
  - Every test that a `docs/features/*/tests.md` cites exists.
  - 1,764 of 1,774 test names follow `x__y__z`.

## Findings

### P0-1: a second order read model, a second order vocabulary and a path collision are planned

- **Where:**
  - `GET /v1/shops/{shop_id}/orders` already exists (`packages/contracts/openapi.yaml:3026`, Phase 4.6). It reads Etsy live and uses `OrderState` from `htco_application/shop_dashboard.py:144`: five states, spelled `canceled`.
  - Slice 5.4 merged a second `OrderState` in `htco_domain/orders.py:20`: eight states with transitions, spelled `cancelled`.
  - Slice 5.6 plans the same path with a different contract (`phase-05-orders-ingest.md:277`).
  - Slice 5.7 plans an "Orders UI", but the clients already ship Orders screens (htco-clients #38, #40).
- **The two classifiers disagree:**
  - The dashboard's `order_state()` (`shop_dashboard.py:1014-1024`) tests `is_paid` before shipped. The domain's `observed_state` (`orders.py:68-80`) tests shipped first. So an unpaid but shipped receipt shows as UNPAID on the screen and as SHIPPED in the store.
  - `_NOT_A_SALE` (`shop_dashboard.py:134`) duplicates `OrderState.counts_as_sale`.
  - The dashboard re-classifies stored rows with its own function (`:1037`, `:1110`, `:1135`) instead of reading the stored state (`persistence/etsy_orders.py:201`).
  - Orders are not in the 5.18 vocabulary (`htco_domain/status.py:74`).
- **Fix (proposal, before 5.6 starts):**
  - Rewrite 5.6 and 5.7 as "move the existing endpoint and screens onto the stored order".
  - Keep one domain `OrderState`. The dashboard maps it to a view-only enum.
  - Delete `order_state()` and `_NOT_A_SALE`, and add orders to `StatusKind`.
  - **Owner:** `p5-pipeline`, after 5.5.

### P1-1: Etsy reads bypass the Action Gateway, and only some bypasses are documented

| Call site | Path | Documented |
|---|---|---|
| `application/order_intake.py:102-134` (`GatewayReceipts`) | Gateway | Yes, the target path |
| `application/execution/channels.py:125-167` | Inside the gateway's API channel | Yes |
| `application/order_history.py:352` (sync in the API process) | Bypass | Yes, a fallback behind `HTCO_ORDER_SYNC_IN_API` until 5.11 (`phase-05:22`) |
| `application/shop_capabilities.py:309-314` (capability probe) | Bypass | Yes, as a follow-up (`phase-05:124`) |
| `application/shop_dashboard.py:818-908` (every dashboard read) | Bypass | **No** |
| `application/etsy_webhooks.py:578,595` (listing and inventory re-reads) | Bypass | **No** |
| `application/etsy_connect.py:311` (`shop_of_user` during connect) | Bypass | **No** |

- **Doc error:** `THREAT_MODEL.md:149` claimed that architecture tests forbid calling channel adapters outside the router. No such test exists.
- **Why it matters:** writes are safe today only because the adapter is GET-only. The first write tool (ADR-0022) removes that guard.
- **Fixed in this PR:** THREAT_MODEL now states the real control and points here for the list.
- **Proposal (Phase 6, before the first write tool):**
  - Add an architecture test that only `execution/channels.py` and the documented legacy callers may hold an `EtsyReader`.
  - Move the webhook re-reads onto `etsy.listing.read`.
  - Record in an ADR-0019 note whether interactive dashboard reads stay direct as live R0 reads.

### P1-2: retries and rate limits are stacked on the worker's gateway path

- **Where:**
  - The adapter retries itself: `EtsyConfig.max_attempts=4` (`integrations/etsy.py:148`), and it waits up to 60 s on a 429 (`:152`, loop at `:596-650`).
  - The gateway retries on top: `InlineRetry` makes 3 calls with a 5 s cap (`execution/guards.py:70-82`, applied at `service.py:500-537`).
  - The worker wires both together (`htco_worker/main.py:297-302`, `:373-377`).
  - `order_history.py:351-364` adds a third 429 loop.
  - Rate limiting is duplicated as well: an in-process throttle and daily floor (`etsy.py:663-700`) alongside `PostgresRateLimiter`.
- **Why it matters:**
  - The worst case is 12 HTTP calls per read action.
  - The adapter's 60 s wait overrides the gateway's 5 s defer rule (`execution-engine.md:452`).
  - Breaker accounting sees one failure where Etsy saw four.
  - The in-process budget is per process, although the docstring at `etsy.py:8` says "per app", so the API and the worker can each use the full per-second limit.
- **Proposal:** the worker builds the gateway's adapter with `EtsyConfig(max_attempts=1, max_retry_after_seconds=0)`, so the gateway owns retries and defers. The budget moves to Postgres when the API reads move behind the gateway. This changes behaviour, so it was not done here. **Owner:** `p5-pipeline` (worker wiring, 5.11).

### P1-3: plan status ticks contradict merged work

The fixes below touch files that #75 also edits, so they were not made here. They are handed to `p5-pipeline` and `plan-update`:

- **`PLAN.md`:**
  - Line 7 still says "PRs #53, #55, #58 open".
  - Line 154 still lists #55 as open and 5.4 as "in review". It omits 5.12, 5.13 and 5.15 to 5.18 (#63, #66, #65, #68, #71, #73).
  - Line 154 carries an "Update 2026-10-24" while line 6 says "Last updated 2026-10-23".
- **`phase-05-orders-ingest.md`:**
  - Line 12 still says "5.1, 5.2 and 5.10 in open PRs".
  - The 5.0 tick (line 18) is unticked, although lines 19, 20 and 28 record it as done.
  - Lines 20 and 60 say "PR #55" where they should say #62.
  - Line 115 numbers the pgTAP files 10, 11, "11_automation_engine". As built, they are 10, 11 and 12.
- **Phase headers:**
  - `phase-01:10` and `phase-03:10` say "In progress"; PLAN §6 says COMPLETE.
  - `phase-02:10` says its exit gate is still open, which contradicts `PLAN.md:151`.
  - Phases 6 to 21 say "Not started", while PLAN says PLANNED. Use one vocabulary.
- **`docs/features/README.md`:**
  - Line 17 says platform-skeleton is "In progress".
  - Line 21 says the worker deploy is "pending", although #54 merged.
  - Line 23 says "channels in 5.13", although 5.13 merged.
  - `auth-bff/` has no row.

### P1-4: the Phase 6 → Phase 7 dependency points the wrong way

- Phase 7 depends on Phase 6 (`phase-07:8`, `PLAN.md:83`).
- Phase 6 needs work that Phase 7 builds:
  - AC-6.7.5 creates "an approval from Phase 7" (`phase-06:141`).
  - 6.10 needs a Human task with a paste field and a "relay item" (`phase-06:170-184`).
- The Phase 5 Human and Approval contracts are in memory only (5.13, 5.14).
- **Proposal (a plan decision for the owner and `phase6`), either:**
  - build 7.1 (tasks) and 7.11 (Postgres approvals and the Human channel) before 6.7 and 6.10; or
  - move 6.10 and AC-6.7.5 into Phase 7.

  In both cases, make "relay item" a `BusinessTask` kind, not a new entity.

### P1-5: exception messages reach logs and Sentry unscrubbed

- **Where:**
  - `apps/api/.../middleware.py:89` calls `_logger.exception("request.failed")`.
  - `platform/logs.py:64` (`dict_tracebacks`) renders the exception value.
  - `platform/error_reporting.py:38-47` (`scrub_event`) drops the request, user and breadcrumbs, but not `exception.values[].value`.
- **Why it matters:** `order_history.py:286` itself notes that database errors can quote buyer rows. A psycopg error with a DETAIL line would carry a buyer's name or address into logs and Sentry.
- **Proposal (small, but it changes runtime behaviour):** add one structlog processor and the matching Sentry `before_send`. Both replace the message of `psycopg.Error`, and of any exception not on an allow-list, with its type and SQLSTATE. Prove it with a log-capture test. **Suggested owner:** `p5-pipeline`, alongside 5.9 observability.

### P1-6: migration versions are future-dated, and nothing checks the order (AC-5.0.1 unmet)

AC-5.0.1 requires "a migration-order test fails if a new migration is older than the newest on `main`" (`phase-05-orders-ingest.md:181`). That test does not exist, so slice 5.0 is still open and ADR-0019 stays Proposed.

- The newest migrations are `20261024100000`, `20261024200000` and `20261024210000`, all merged on 2026-10-23. A migration stamped with the real time sorts before them, and `supabase db push` refuses it as out of order.
- pgTAP prefix `14` is used twice: `14_action_guards` and `14_worker_order_purge`. Slice 5.0 was meant to prevent exactly this collision.
- **Proposal:**
  - Add a rule to ENGINEERING_RULES §data: "version = max(now, newest on main + 1)".
  - Add a check to `tools/test_integrity.py` that a PR's new migrations sort after the newest one on `main`, and that pgTAP prefixes are unique.
  - Renaming the pgTAP file is a one-line follow-up once #75 (which adds `18_…`) merges.

### P1-7: six tables with RLS have no pgTAP test as `authenticated`

TESTING_STRATEGY §8 requires an RLS test for every exposed table. Each table below has an `authenticated` select policy, but its pgTAP file never runs `set local role authenticated`:

| Table | Policy | Test file |
|---|---|---|
| `app.etsy_order_items` | `20261022120000:178` | `08` only checks `relrowsecurity` |
| `app.etsy_order_sync_state` | `20261022120000:188` | `08` |
| `app.automation_executions` | `20261023140000:197` | `12` runs as the worker only |
| `app.action_executions` | `20261023150000:115` | `13` runs as the worker only |
| `app.order_status_events` | `20261024210000:124` | `17` runs as the worker only |
| `app.financial_entries` | `20261024210000:129` | `17` runs as the worker only |

**Proposal:** add the pattern from files `07` and `16`: own organization sees its rows, another organization sees 0, and a role without buyer data sees 0. Tests 08 and 17 overlap #75, so they go to `p5-pipeline`. Tests 12 and 13 can be a separate test-only PR.

### P2-1: status enums outside the 5.18 vocabulary

`htco_domain/status.py:39-74` maps four enums: `ExecutionState`, `ActionState`, `EnvironmentStatus` and `TaskState`. These are not mapped:

- `JobState` (`domain/jobs.py:24`). It says `DEAD`, while `ExecutionState` says `DEAD_LETTERED`.
- `ApprovalState` (`domain/approvals.py:21`).
- `HumanTaskState` (`application/execution/channels.py:291`). It is defined in the application layer and duplicates `TaskState`.
- `SessionStatus`, both `OrderState` enums and `IntegrationStatus`.

**Proposal:**

- Move `HumanTaskState` into the domain, or alias it to `TaskState`.
- Add `JobState`, `ApprovalState` and `OrderState` to `_KINDS`, with the coverage test.
- Do this as a small slice right after 5.5.

### P2-2: future phases that would create parallel subsystems

Each item says what to write into the plan now:

1. **7.10 "Scheduling & Recovery"** (`phase-07:157-164`) rescans expired leases and stalled executions. The queue (5.1) and the engine (5.10) already do this. Limit 7.10 to `BusinessTask` SLA scans, run as a 5.1 queue schedule.
2. **Three alerting paths:** 5.9 Sentry rules (`phase-05:316`), 8A Grafana (`phase-08a:161,168`) and Phase 10 monitors (`phase-10:40,86`, which duplicate AC-5.9.1). There is also no stated link between Phase 6 alerts, incidents and notifications (6.9, 6.3, 6.4). Proposal: Phase 10's declared monitors are the one alert model, and their outputs are incidents or notifications through 6.4. 5.9 and 8A only expose metrics.
3. **New state machines outside 5.18:** the 6.3 incident machine (`phase-06:94`) and the 7.13 `quote_requests` states (`phase-07:205`) are not mapped to `StatusKind`. Add "states map to `htco_domain.status.StatusKind`, with a coverage test" to both.
4. **Four or five trail writes per task change:**
   - Phase 7 writes `task_events` (7.1), `tasks.state_changed.v1` (7.11) and `activity_records` (7.7), on top of the audit (2.5) and the 5.18 events.
   - 8.10 requires both `action_executions` and `tool_invocations` for one call (`phase-08:164`).
   - Proposal (needs an ADR): `activity_records` is derived from the 5.18 outbox events, and `tool_invocations` is a view on `action_executions`.
5. **Approvals built twice in Phase 7.** 7.3 creates an "Approval aggregate", and 7.11 puts approvals on Postgres on the 5.14 contract. Fold 7.3 into 7.11.
6. **The tool bridge is built twice:**
   - 8.4 gives the bridge its own checks, and 8.10 then moves it onto the gateway. Merge the two slices.
   - Two catalogs coexist: `registry/tools/` with `tool_definitions`, and `registry/actions/` with `ActionDefinition`. State that actions are the source: tool plus bindings (ADR-0019 §1).
7. **8A "Browser DLQ"** (`phase-08a:39`) duplicates the jobs `dead` state. Write "browser DLQ = jobs in `dead` with kind `browser.*`".
8. **Two daily reports:** 7.9 builds the operations summary, and Phase 10 builds `daily_operations_reports` (`phase-10:126`). Phase 10 should extend 7.9.
9. **Future order stores:** `autova_orders` (8A A-01, Phase 13) and the Phase 21 channel-neutral `orders`. `autova_orders` references `etsy_orders`, and the D-19 `orders` stays a view.

### P2-3: Problem types have no single registry

- About 56 slugs are spread over roughly 15 modules.
- Two slugs are defined twice:
  - `forbidden` in `htco_api/identity.py:47` and `policy_guard.py:19`.
  - `mfa-required` in `access.py:53` and `policy_guard.py:20`, with different `detail` texts.
- Eight slugs are in neither the docs nor `openapi.yaml`: `idempotency-key-invalid`, `idempotency-key-required`, `invitations-unavailable`, `member-exists`, `mfa-not-enrolled`, `session-ended`, `sign-in-refused` and `user-already-registered`.
- `problem_type_for_status` (`problems.py:157-163`) creates slugs from the HTTP phrase at run time.
- Each router has its own error mapper: `shop_data.py:750`, `automations.py:197`, `access.py:135`, and about 20 `except` clauses in `etsy.py:409-575`.
- **Proposal:**
  - One registry in `problems.py`.
  - A test that collects every `ProblemType`, asserts that each slug has one (title, status), and checks it against a `docs/architecture/problem-types.md` table.
  - Import `FORBIDDEN` and `MFA_REQUIRED` from `policy_guard` (this changes the `access.py` 403 detail text).
  - A Phase 5 hygiene slice.

### P2-4: Supabase HTTP adapters live in `htco_platform`

- `htco_platform/supabase_auth.py` and `supabase_admin.py` call an external system over HTTP. SOFTWARE_DESIGN §4.1 puts external systems in integrations.
- Their error glue sits in the composition root (`htco_api/main.py:239+`, `_Inviter`).
- Two classes are named `InvitationFailedError` (`supabase_admin.py:27`, `access.py:76`), told apart by an alias at `main.py:95`.
- **Proposal:** a mechanical move to `htco_integrations`. A Phase 6 cleanup.

### P2-5: dead code that swallows errors

- The old in-process `AutomationEngine` and `ExecutionStore` (`automation/engine.py:30,89`) are used only by `tests/application/test_automation_engine.py`. Production uses `DurableAutomationEngine`.
- `engine.py:141` is a bare `except Exception:` that records nothing.
- Two docstrings are stale: `engine.py:8-9` and `automations.py:6`.
- TESTING_STRATEGY §10 cites the old file for SCN-B and SCN-G.
- `SecretBroker.attach_codes` (`secrets.py:274`) has no callers.
- **Proposal:** move SCN-B and SCN-G onto the durable engine, then delete the class. Coordinate with 5.11.

### P2-6: configuration documentation

- `.env.example` said it listed every variable, but 11 were missing.
- **Fixed in #77:** they are added, with a test that keeps the file complete.
- Two flags default to on: `order_sync_in_api=True` and `worker_action_gateway="reads"`. Both are documented, and 5.11 retires the first.

### P2-7: missing import contracts

- **Fixed in #76:** four contracts that hold today:
  - `httpx` only in integrations, platform and the composition roots;
  - the web framework only in `htco_api`;
  - no Pydantic in the adapters;
  - routers never import adapters.
- SOFTWARE_DESIGN §4.3 lists them.
- The router contract covers the whole `htco_api` package, exempting only `main` and `cli`, once #80 (the follow-up to #76 after its Codex review) merges. Until then, a new router module must be added to its list.

### P3 findings

- **IDs and time are not always injected (ER-CODE-08):**
  - Nine direct `uuid.uuid7()` calls in application code: `approvals.py:178`, `audit.py:47`, `execution/channels.py:333`, `execution/service.py:767`, `status.py:57`, `web_sessions.py:384`, `secrets.py:304` and `:496`, and `etsy_connect.py:388`.
  - `supabase_auth.py:247` calls `datetime.now`.
  - The clock lambda is repeated six times (`htco_api/main.py:460,492,524,534,555`, `app.py:142`).
  - SOFTWARE_DESIGN:108 promises a `system_clock` in `htco_platform`.
  - Fix with a `new_id` parameter at each site.
- **File I/O in the application layer:** `workforce/files.py:33-34` reads TOML with `pathlib`. Move it behind a `RegistrySource` port.
- **Broad `except Exception`:** `shop_dashboard.py:814,947,963`, `order_history.py:285,291`, `api/etsy.py:309`, `integrations/etsy.py:792` and `main.py:217`. All of them log the type and degrade on purpose. Amend ER-CODE-05 to allow "best-effort side calls, logged by type", or narrow them.
- **`decisions/advisor.py:125`** stores `str(error)` from provider exceptions in `DecisionRecord`. Use `describe_error`. It is test-only until Phase 9.
- **Live-safety of migrations:**
  - Indexes on existing tables are built without `CONCURRENTLY`: `20261015180000:42`, `20261016090000:75`, `20261023090000:18` and `20261024100000:17`.
  - `20261024210000:22-28` adds CHECK constraints, which take ACCESS EXCLUSIVE and scan the table.
  - All of this is fine at today's sizes. Require a "Live-safety:" header line in each migration.
- **Weak assertion:** `tests/integration/test_order_intake.py:224` uses `pytest.raises(Exception)`. Name `InsufficientPrivilege`. This is in the order area (#75).
- **AC marker coverage:** 107 of 389 AC IDs carry `@pytest.mark.ac`.
  - Open ACs in done slices: AC-1.6.2, AC-1.7.3, AC-4.7.2 (restore rehearsal), AC-3.5.6 (email leg) and AC-5.1.5.
  - AC-4.6.1 and AC-4.6.2 have no `tests.md` row.
- **Naming:**
  - `BROWSER_ENVIRONMENTS_OFF` means `browser-environments-unavailable`.
  - Refusal codes are snake_case (`approvals.py:150`), while problem slugs are kebab-case.
  - One idea has three names: `buyer_data`, `buyer_details` and `with_buyer`.
- **Feature folders:** `docs/features/auth-bff/` has no `design.md`; `secret-broker/` has no `design.md` or `operations.md` (ER-DOC-02).
- **Diagnostics route:** `POST /_diagnostics/error` is unauthenticated outside production (`main.py:176`). It is documented, but it lets anyone spend the Sentry quota in staging.
- **ADRs:**
  - ADR-0005 decides Supabase Pro, but production runs Free (`PLAN.md:151`). Add a validation-log line.
  - D-19 is still "Open" (ASSUMPTIONS:32), although 5.4 built on `etsy_orders` (AC-5.4.6). The owner should mark (a) as decided.
- **PR #47** (auth throttles) has been open since Phase 3 and is not tracked in any plan file.

## Fixed with this audit

| PR | Change |
|---|---|
| #76 | Four import-linter contracts; SOFTWARE_DESIGN §4.3 |
| #77 | `.env.example` lists all API and worker variables; `tests/tools/test_env_example.py` |
| This PR | ADR-0019 validation log: why it is still Proposed (AC-5.0.1), and the tooling deviations. The ADR index row for ADR-0003 now says "to be validated". THREAT_MODEL gateway row corrected. EXECUTION_ORDER: D-22 and D-18 marked decided, #55 marked replaced by #62 |

## Proposed refactors, with a suggested phase

| Refactor | Phase |
|---|---|
| One domain `OrderState`; 5.6 and 5.7 rewritten onto the stored order (P0-1) | 5, before 5.6 |
| The gateway owns retries; the adapter is built with a single attempt in the worker (P1-2) | 5.11 |
| Log and Sentry scrubbing of exception messages (P1-5) | 5.9 |
| A migration order check (finishes slice 5.0 and accepts ADR-0019), unique pgTAP prefixes, the "Live-safety:" header (P1-6) | 5, next |
| `authenticated` RLS tests for six tables (P1-7) | 5 |
| Status vocabulary: jobs, approvals and orders (P2-1) | 5, after 5.5 |
| Architecture test for gateway bypass; webhook re-reads through actions (P1-1) | 6, before ADR-0022 writes |
| Fix the Phase 6/7 dependency (P1-4), one alert model and event trail (P2-2) | Plan, before 6.3, 6.7 and 6.10 |
| Problem-type registry and test (P2-3) | 5 hygiene |
| Supabase adapters moved to integrations (P2-4) | 6 cleanup |
| Retire `AutomationEngine` (P2-5) | 5.11 |
