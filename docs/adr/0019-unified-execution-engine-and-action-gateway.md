# ADR-0019: One execution engine and one Action Gateway for automations and AI employees

- **Status:** Accepted 2026-10-23: slice 5.0 is complete, with tests for AC-5.0.1 (CI's `migration-order` job) and AC-5.0.2 (the correlation trace) (proposed with the owner's architecture upgrade brief, 2026-10-23)
- **Date:** 2026-10-23
- **Deciders:** Owner, engineering
- **Amends:** [ADR-0013](0013-automation-engine.md) (the engine gains a gateway and four channels), [ADR-0014](0014-tool-registry.md) (tool calls of every actor pass the gateway), [ADR-0007](0007-job-queue.md) (the queue is the engine's only transport; optional Valkey), [ADR-0003](0003-agent-runtime-langgraph.md) (the LangGraph Postgres checkpointer is named; LangGraph never runs deterministic work)
- **Related:** [ADR-0020](0020-per-shop-browser-environment.md) (browser environment), [ADR-0021](0021-authenticator-service.md) (authenticator), [ADR-0022](0022-etsy-write-tools-and-browser-scope.md) (Etsy writes and browser scope), [execution engine design](../architecture/execution-engine.md)

## Context

The owner's brief of 2026-10-23 asks for **one shared execution infrastructure** for every deterministic automation and every AI employee. Work must be able to run through Etsy's API, through a browser on behalf of a shop, as internal code, or by a person. The current plan has the pieces but no single path for them:

- the durable automation engine ([ADR-0013](0013-automation-engine.md), PR #58);
- the job queue, inbox and outbox (PRs #53 and #55);
- the tool bridge for employees (Phase 8);
- approvals (Phase 7).

Without one path, each channel would grow its own permissions, retries, rate limits and audit, and the browser channel would be a second, weaker way to act on a shop.

## Decision drivers

- **One place decides.** A permission, an approval rule or a rate limit is enforced once, whoever calls: an automation, an employee, a user or a schedule.
- **Deterministic work stays deterministic.** No model call and no LangGraph session in an automation's path, ever (ADR-0012 rule 3, ADR-0013).
- **Channel independence.** An action is declared once. Whether it runs over the API, a browser or a human task is a routing decision, not a new implementation.
- **No duplicate infrastructure.** Reuse the queue, inbox, outbox, idempotency keys, execution records and audit that Phase 5 already built.
- **Free or OSS components.** Postgres first; anything else must be optional.

## Options considered

1. **Keep the channels separate.** Automations use the engine; employees use the tool bridge; browser work gets its own worker. Fastest to start, but it means three permission paths and three audit trails.
2. **An external workflow platform** (Temporal, Inngest, n8n) for every channel. Strong durability, but another platform to run, and it overlaps with what PRs #53, #55 and #58 already built.
3. **One Action Gateway in front of one Execution Router**, with four channel adapters, built on the Phase 5 queue and engine.

## Decision

Option 3.

1. **Action Gateway** (`htco_application.execution.gateway`). This is the single entry point for every business action. The automation engine's handlers call it, the Phase 8 tool bridge calls it, and so do user-triggered actions. For each `ActionRequest` it:
   - resolves the `ActionDefinition`, which is the tool definition plus its channel bindings;
   - checks the policy engine and tool access (ADR-0014). The actor's grants, the shop's grants, and for employees the originating user's access all apply, and the narrowest wins;
   - applies the approval rule, raising `REQUIRE_APPROVAL` → approval → resume;
   - claims the idempotency key;
   - applies the circuit breaker, rate and concurrency limits;
   - routes the action, verifies the result, and records the audit and activity records.

   Deterministic automations call the gateway in-process, with no model involved. Employees reach it only through the tool bridge, never directly.
2. **Generic tool execution contract.** Every action carries the same envelope:
   - `ActionRequest`: action, version, actor, tenant, shop, idempotency key, correlation and causation IDs, validated input, requested channel, deadline;
   - `ActionResult`: outcome, verified output, evidence, channel used, attempts, cost.

   Each action has a Pydantic input and output model, published as JSON Schema in `packages/contracts/actions/`. The signatures are in [execution-engine.md](../architecture/execution-engine.md).
3. **Execution Router and four channels.** The router picks the channel from the action's bindings, the shop's capabilities and the environment's status.

   | Channel | What runs | Built in |
   |---|---|---|
   | **API** | Etsy through the existing httpx adapter and OAuth vault (ADR-0008), with writes through the outbox | Phase 5 (real adapter) |
   | **Browser** | A shop's Browser Environment ([ADR-0020](0020-per-shop-browser-environment.md)): Playwright for deterministic steps, Browser Use OSS as the browser agent, and Jev for small proven decisions | Phase 5 (interface and fake), Phase 8A (real) |
   | **Internal** | Python use cases and database work | Phase 5 |
   | **Human** | A task or approval for a person: manual login, an unsupported authentication method, a CAPTCHA, a refund in Etsy. The engine resumes from its checkpoint when the person finishes | Phase 5 (contract), Phase 7 (tasks and approvals) |

   A channel is never chosen to get around a refusal. If the API refuses an action for lack of a scope or a capability, the browser channel is used only when the action has its own browser binding inside the recorded Etsy scope ([ADR-0022](0022-etsy-write-tools-and-browser-scope.md)).
4. **Deterministic automations use no LangGraph and no model.**
   - An automation's steps run as an `AutomationExecution` on the job queue.
   - The registry keeps `ai_steps = ()` for every automation (tested in `tests/tools/test_registry_files.py`). A step that needs reasoning becomes a task for an employee, through the Human or employee path, never a model call inside the automation.
   - The browser channel's Browser Use agent is an LLM. An automation may use the browser channel only through **deterministic Playwright recipes**. A recipe that needs Browser Use is an employee's action, under that employee's routing policy and budget.
5. **One durable substrate.**
   - The Phase 5 job queue (ADR-0007) carries every execution. Idempotency lives in Postgres: `jobs.idempotency`, the engine's `(automation_id, idempotency_key)` and the new `action_executions`.
   - Postgres advisory locks serialize the work of one shop's browser profile.
   - `aiolimiter` (in-process) with a Postgres token bucket enforces Etsy's per-app limit across workers. Valkey is an **optional** later backend for the limiter and coordination, added only on measured need.
   - Tenacity handles retries inside one attempt, PyBreaker the circuit breakers, structlog the logs, and Prometheus with Grafana OSS the metrics.
   - The LangGraph Postgres checkpointer serves employees' sessions only (Phase 8).
6. **Approvals are shared.** One approval contract serves employees, automations, users and the browser channel ([execution-engine.md §5](../architecture/execution-engine.md#5-approval-contract)), built in Phase 7. Phase 5 defines it and ships an in-memory implementation for tests.

## Consequences

- Positive: one permission, approval, rate-limit and audit path for every actor and channel. Browser and human work become ordinary, observable executions. The Phase 5 PRs are reused, not replaced.
- Negative: the gateway is on every path, so its latency and correctness matter. Its tests (Scenario H, replay and approval) become release blockers. A little indirection is added for simple internal actions.
- Follow-ups:
  - Phase 5 aligns PRs #53, #55 and #58 with the contracts (list in [Phase 5](../plan/phases/phase-05-orders-ingest.md#alignment-of-the-open-pull-requests));
  - the Phase 8 tool bridge becomes a client of the gateway;
  - Phase 8A builds the real browser adapter.

## Validation

- Scenario G (one order, many replays) and Scenario H (denied tool) pass through the gateway against Postgres in Phase 5.
- A contract test runs one action through the API, Internal and fake Browser adapters with the same envelope and audit.
- Phase 8 repeats Scenario H through the tool bridge.

## Validation log

- 2026-10-23 — Proposed with the plan update. The contracts and state machines are specified in [execution-engine.md](../architecture/execution-engine.md).
- 2026-10-23 — Still proposed. Slices 5.1, 5.2, 5.4, 5.10 and 5.12 to 5.18 are built on it, but slice 5.0 is not complete. Two of its criteria have no test yet ([architecture audit](../reviews/2026-10-23-architecture-audit.md), P1-6):
  - AC-5.0.1: the migration-order test does not exist.
  - AC-5.0.2: no test traces one correlation ID from trigger to execution to job to outbox. `tests/application/test_messaging.py:186` checks only the outbox-to-delivery-job hop.

  Accept the ADR when every 5.0 criterion is met. Two owner-approved tooling deviations from Decision §2 and §5 are recorded in [execution-engine.md](../architecture/execution-engine.md) §2 and §8: standard-library dataclass contracts instead of Pydantic models, and in-house breaker, limiter and retries instead of PyBreaker, aiolimiter and Tenacity. Those sections take precedence where this ADR names the libraries. The architecture audit of the same day ([review](../reviews/2026-10-23-architecture-audit.md)) lists the Etsy reads that still bypass the gateway.
- 2026-10-23 — Accepted. Slice 5.0 is complete:
  - AC-5.0.1: `tools/migration_order.py`, run by CI's `migration-order` job, fails a pull request whose new migration sorts before or with the newest on main, renames a merged migration or reuses a pgTAP prefix (ER-DATA-10). The duplicate pgTAP prefix 14 is gone (`14_worker_order_purge` became `20_worker_order_purge`).
  - AC-5.0.2: `tests/integration/test_correlation_trace.py` follows one correlation ID through the execution, its job, the action execution, the outbox events and the execution's state changes, against Postgres. The execution job now carries the correlation ID, with the execution as its cause (it carried neither before).
- 2026-10-26 — Amended by [ADR-0025](0025-browser-and-network-providers.md) §3 (architecture audit AUD-04). The Browser channel no longer awaits the browser inline:
  - **Dispatch.** It moves the action to `DISPATCHED`, a waiting outcome, and enqueues one `browser.run` job for the browser worker. The waiting execution then holds no lease, slot or connection.
  - **Execution-time re-checks.** Before acting, the browser worker checks the claim, the deadline, the status, the flag, the scope and every kill switch.
  - **Outcome.** It publishes one outcome.
  - **Resume.** A resume resubmits the same request, and `submit` continues the stored claim, as it already does for a human hand-off.
  - **Lost writes.** A write is never re-sent blindly.

  Decision §3's Browser row and the action state machine in [execution-engine.md](../architecture/execution-engine.md) §7 follow this.
- 2026-10-27 — The AUD-04 design is complete (#143, AC-1). ADR-0025 §3 is the design; this entry maps #143's criteria onto it and settles the four points it left open. The code (#143 AC-2 to AC-5) follows in `htco_application.execution` once orion's `execution/` work (#147, #161) has merged.

  | #143 AC-1 asks for | Where it is decided |
  |---|---|
  | A dispatched outcome | `DISPATCHED`, a waiting outcome (`StatusKind.waiting`, reason `waiting_for_browser`): ADR-0025 §3 step 1, execution-engine.md §7 |
  | The `browser.*` job kind and its payload | `browser.run` with a locator payload only (execution ID and attempt); the routed binding and the request hash live on the stored claim, never a secret: §3 step 1 |
  | One lock per browser profile | The job's concurrency key `browser:{environment_id}`, and the profile's advisory lock in the controller: §3 steps 1 and 6 |
  | How the worker reports a result | The run's row (set once through `private.finish_browser_run`) and `browser.run_finished.v1` in the outbox: §3 step 4 |
  | How the action resumes | A `browser.resume` job resubmits the same request; `submit` continues the stored claim, as for a human hand-off: §3 step 5 |
  | Timeouts and the slot's lease | The gateway's slot is released at dispatch; the deadline watchdog (deadline plus grace, grace at least the session's maximum) resolves every attempt: §3 steps 1, 2 and 6 |

  **Settled here:**
  1. **Completions that change nothing (#143 AC-3).** A resume applies an outcome only to an action that is `DISPATCHED`, for the attempt and the request hash of its stored claim. The resume ignores three kinds of completion, and records each one: a log line with the code below, and `actions.resume_ignored.v1` with the execution, the attempt and the code, through the outbox. Nothing else changes. The event's JSON Schema and its EVENT_CATALOG row land with that code, so the event-contract test of #174 covers it.
     - `duplicate`: the outcome of this attempt was already applied. The resume answers the recorded result, as `submit` does for any repeated key.
     - `cancelled`: the action is `CANCELLED` (an approval revoked or expired before the attempt started, ADR-0025 §3 step 4a) or already final.
     - `hash_mismatch`: the outcome names a request hash, or an attempt, that is not the stored claim's. This one also opens an incident, because only a bug or a forged row can cause it.
  2. **The breaker and the rate budget (#143 AC-5).** Both apply at dispatch, per the binding's keys:
     - the `browser:{shop}` breaker is checked at dispatch (open means `DEFERRED`, as for any channel);
     - the `browser:{shop}` rate window is spent at dispatch;
     - the browser worker reads the same breaker again at execution time, next to the kill switches, so a backlog of `browser.run` jobs dispatched just before it opened is stopped too (AC-8A.2.30);
     - the resume records the run's result on that breaker, and **only a run that started on the site counts** (quill): a session that failed with the controller's `timeout` or `error`, and an `interrupted` run, count as a failure. These never count, because they say nothing about the shop's site, or, like a 4xx on the API channel, they are handled elsewhere:
       - the watchdog's `timeout` for an attempt that never started (the profile was busy, no worker slot was free, or the browser worker was down), so a backed-up HTCO worker never opens a healthy shop's breaker;
       - `deferred`: the profile was busy, or no parallel slot was free by the deadline;
       - `lock_lost`: the profile lock's database connection dropped, which is our infrastructure, not the shop's site;
       - a capability refusal (`CapabilityRefusedError`), raised before any session starts;
       - `challenge` and `refused`, which the challenge counter and suspension handle.

     The concurrency slot is the job's (`browser:{environment_id}`), held by the queue while the browser worker runs it, never by the gateway call. Separately, each browser worker runs at most `ProviderCapabilities.parallel_profiles` sessions at once (the controller's slots, AC-8A.2.8), so a claimed `browser.run` may wait for a worker slot until its deadline.
  3. **The browser worker's handlers (#143 AC-6).** They make no blocking database call on the event loop. Every store they use is an async adapter, or its calls are offloaded to a worker thread, as AUD-05 (#150) asks at the store boundary.
  4. **Where the browser channel lives.** In the agent-worker, `BrowserChannel` only dispatches. Like every channel, it exposes `.recipes`, so the startup check of #147 (an enabled binding without a recipe refuses to start) covers browser bindings too. The browser worker never calls the gateway. Each recipe also carries its provider needs; the browser worker's startup check that they are a subset of its provider's capabilities belongs to 8A.5, with the recipes.

  This entry records evidence of the settled design. Further design refinements go in [execution-engine.md](../architecture/execution-engine.md) or the plan's acceptance criteria, not in this log.
