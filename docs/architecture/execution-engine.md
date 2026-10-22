# The Unified Automation & Execution Engine

| | |
|---|---|
| **Status** | Design 1.0 (2026-10-23). Contracts are defined in Phase 5, the Browser adapter is built in Phase 8A, and the Authenticator in Phase 8B |
| **Decisions** | [ADR-0019](../adr/0019-unified-execution-engine-and-action-gateway.md) gateway and channels · [ADR-0020](../adr/0020-per-shop-browser-environment.md) browser environment · [ADR-0021](../adr/0021-authenticator-service.md) authenticator · [ADR-0022](../adr/0022-etsy-write-tools-and-browser-scope.md) Etsy writes and browser scope |
| **Builds on** | Job queue (ADR-0007, PR #53), inbox and outbox (PR #55), durable automation engine (ADR-0013, PR #58), tool registry (ADR-0014), policy engine (Phase 7) |

This page holds the contracts that Phase 5 defines and later phases implement: Python `Protocol` signatures, Pydantic contract shapes, the end-to-end flow, and the state machines for tasks, executions and browser environments. The signatures are the target for the code. Names may still change in review; the meaning must not.

## 1. The flow

```text
 Shop added ──► Etsy OAuth (Phase 4) ──► [optional] Create Browser Environment
                                             │  PROVISIONING: profile, credentials (vault),
                                             │  TOTP (Authenticator), preflight + health check
                                             ▼
                                           READY
 Triggers
   event (inbox: Etsy webhook, email)  ┐
   schedule (job queue)                │
   user action (API, Idempotency-Key)  ├──► AutomationEngine.trigger ──► AutomationExecution
   agent tool (tool bridge, Phase 8)   │        (deterministic, no model)        │ each business step
   another automation                  ┘                                         ▼
                                                      ┌──────────────── ACTION GATEWAY ────────────────┐
   employee session ──► tool bridge ─────────────────►│ 1 resolve ActionDefinition (tool + bindings)   │
                                                      │ 2 policy engine + tool access (narrowest wins) │
                                                      │ 3 approval rule ─► REQUIRE_APPROVAL ─► Human   │
                                                      │ 4 idempotency claim (Postgres)                 │
                                                      │ 5 circuit breaker · rate · concurrency         │
                                                      └───────────────────────┬────────────────────────┘
                                                                              ▼
                                                                     EXECUTION ROUTER
                                   ┌──────────────────┬───────────────────┬─┴────────────────┐
                                   ▼                  ▼                   ▼                  ▼
                                  API              BROWSER             INTERNAL            HUMAN
                          httpx Etsy adapter   Browser Controller    use cases, SQL     task / approval
                          OAuth vault, outbox  advisory lock/profile                     manual login,
                                               preflight → Playwright                     CAPTCHA, refund
                                               (deterministic) or Browser Use             → resume from
                                               (employees only) · Secret Broker           checkpoint
                                   └──────────────────┴───────────────────┴──────────────────┘
                                                                              ▼
                                      verify result (re-read / schema) ─► record: CRM tables,
                                      audit_log, activity_records, action_executions ─►
                                      checkpoint (execution state; LangGraph only for sessions) ─►
                                      cleanup (close context, release lock, drop secrets) ─► next job
```

Failure paths:

- a transient error is retried within the attempt (Tenacity), then by the job's retry policy;
- an open circuit breaker defers the job;
- a security challenge stops the action and opens a Human task;
- exhausted retries send the job to the DLQ, which is `dead` jobs and `DEAD_LETTERED` executions, visible in the admin view.

## 2. Execution contract (Pydantic)

These are the shapes of `packages/contracts/actions/*.json`, generated from Pydantic v2 models in `htco_application.execution.contracts`.

```python
class Channel(StrEnum):
    API = "api"
    BROWSER = "browser"
    INTERNAL = "internal"
    HUMAN = "human"

class ActorKind(StrEnum):            # existing htco_domain.actors
    USER = "user"; EMPLOYEE = "employee"; AUTOMATION = "automation"
    ADVISOR = "advisor"; SYSTEM = "system"

class ActionRef(BaseModel):
    name: str                        # tool name, e.g. "etsy.inventory.update"
    version: int

class ActionRequest(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    action: ActionRef
    actor: ActorRef                  # kind + id; from the authenticated context, never from model output
    on_behalf_of_user: UUID | None   # the originating user for employee sessions (narrowest access wins)
    organization_id: UUID
    shop_id: UUID | None
    idempotency_key: str             # 1..200 chars, deterministic per business effect
    correlation_id: str
    causation_id: str | None
    input: dict[str, JsonValue]      # validated against the action's input model before routing
    channel_hint: Channel | None = None   # a preference only; the router decides
    deadline: AwareDatetime | None = None
    approval_id: UUID | None = None  # set when resuming after a decided approval
    owner_trigger: bool = False      # the Owner ran it by hand (first-run rule, ADR-0022)

class Evidence(BaseModel):
    kind: Literal["api_response", "reread", "dom_snapshot_hash", "export_file", "human_note"]
    ref: str                         # storage key or row ID; never secrets or raw buyer content
    sha256: str | None = None

class ActionOutcome(StrEnum):
    SUCCEEDED = "succeeded"; FAILED = "failed"; DENIED = "denied"
    PENDING_APPROVAL = "pending_approval"; HANDED_TO_HUMAN = "handed_to_human"
    DEFERRED = "deferred"            # breaker open or rate-limited; re-queued

class ActionResult(BaseModel):
    model_config = ConfigDict(frozen=True)
    execution_id: UUID
    outcome: ActionOutcome
    channel: Channel | None
    output: dict[str, JsonValue] | None    # validated against the action's output model
    verified: bool                   # the effect was re-read or validated
    evidence: tuple[Evidence, ...] = ()
    attempts: int
    error_code: str | None = None    # stable codes: "scope_missing", "capability_blocked", "challenge", ...
    cost: Decimal | None = None      # model or paid-API cost, if any

class ChannelBinding(BaseModel):
    channel: Channel
    recipe: str | None = None        # browser: recipe ID from the catalog; api: adapter method
    requires_capability: str | None = None   # e.g. "etsy.shipments.create" from shop_capabilities
    browser_scope_id: str | None = None      # e.g. "B-02" from etsy-automation-approval.md
    deterministic: bool = True       # False only for Browser Use recipes (employees only)

class ActionDefinition(BaseModel):
    tool: ToolDefinitionModel        # the registry's ToolDefinition (side effect, risk, data scope)
    input_schema: str                # JSON Schema ID
    output_schema: str
    bindings: tuple[ChannelBinding, ...]     # ordered by preference
    approval_policy: Literal["R0", "R1", "W1", "W2", "W3"]
    rate_limit_key: str | None       # e.g. "etsy:app" (shared app budget), "browser:{shop}"
    concurrency_key: str | None      # e.g. "shop:{shop_id}:writes"
    verify: Literal["none", "reread", "schema", "human"]
    timeout: timedelta
```

## 3. Ports (Python Protocols)

```python
class ActionGateway(Protocol):
    async def submit(self, request: ActionRequest) -> ActionResult:
        """The only entry point for business actions. Idempotent per
        (action.name, idempotency_key): a replay returns the first result."""

class ExecutionRouter(Protocol):
    def choose(self, definition: ActionDefinition, ctx: RoutingContext) -> ChannelBinding:
        """Deterministic: bindings in order, filtered by shop capabilities, environment
        status, browser scope flags and actor kind (automations: deterministic only)."""

class ChannelAdapter(Protocol):
    channel: ClassVar[Channel]
    async def execute(self, request: ActionRequest, binding: ChannelBinding,
                      lease: ExecutionLease) -> ChannelOutcome: ...
    async def verify(self, request: ActionRequest, outcome: ChannelOutcome) -> Verification: ...

class ApiChannel(ChannelAdapter, Protocol): ...          # Etsy httpx adapter; writes via outbox
class InternalChannel(ChannelAdapter, Protocol): ...     # use cases
class HumanChannel(ChannelAdapter, Protocol):            # tasks and approvals (Phase 7)
    async def hand_off(self, request: ActionRequest, reason: HandoffReason) -> UUID: ...

class BrowserChannel(ChannelAdapter, Protocol):          # Phase 5: interface + FakeBrowserChannel
    async def preflight(self, environment_id: UUID) -> PreflightReport: ...

class BrowserController(Protocol):                       # Phase 8A
    @asynccontextmanager
    def session(self, environment_id: UUID, *, purpose: str,
                deadline: AwareDatetime) -> AsyncIterator[BrowserSession]:
        """Holds pg advisory lock (profile), unpacks the sealed profile, starts one engine
        (Playwright persistent context or CDP Chromium), runs preflight; on exit closes,
        re-seals the profile, drops secrets and releases the lock."""

class BrowserSession(Protocol):
    async def run_recipe(self, recipe: str, params: Mapping[str, JsonValue]) -> RecipeOutcome: ...
    async def extract(self, recipe: str, model: type[BaseModel]) -> BaseModel: ...
    async def inject_secret(self, grant: InjectionGrant, selector: str) -> None: ...

class ShopEnvironmentRegistry(Protocol):
    def get(self, shop_id: UUID) -> BrowserEnvironment | None: ...
    def create(self, shop_id: UUID, *, by: ActorRef, consent: ConsentRecord) -> BrowserEnvironment: ...
    def transition(self, environment_id: UUID, target: EnvironmentStatus, *,
                   reason: str, by: ActorRef, expected: EnvironmentStatus) -> BrowserEnvironment: ...
    def record_health(self, environment_id: UUID, report: HealthReport) -> None: ...

class EnvironmentProvisioner(Protocol):                  # contract in Phase 5, real in 8A
    async def provision(self, environment_id: UUID) -> ProvisioningResult: ...
    async def deprovision(self, environment_id: UUID) -> None: ...

class SecretBroker(Protocol):                            # contract in Phase 5, real in 8B
    def issue_injection_grant(self, *, execution_id: UUID, environment_id: UUID,
                              secret_ref: SecretRef, field: str, ttl: timedelta) -> InjectionGrant: ...
    def redeem(self, grant: InjectionGrant, *, worker: ActorRef) -> SecretValue:
        """Browser workers only; one use; audited; SecretValue has no __repr__/__str__."""

class Authenticator(Protocol):                           # Phase 8B
    def enrol(self, *, shop_id: UUID, label: str, secret_or_uri: SecretValue,
              confirm_code: str, by: ActorRef, consent: ConsentRecord) -> TotpAccount: ...
    def rotate(self, account_id: UUID, *, new_secret: SecretValue, confirm_code: str,
               by: ActorRef) -> TotpAccount: ...
    def remove(self, account_id: UUID, *, by: ActorRef) -> None: ...
    def current_code_for_display(self, account_id: UUID, *, viewer: AuthContext) -> DisplayedCode: ...
    def code_for_injection(self, grant: InjectionGrant) -> SecretValue: ...

class ApprovalService(Protocol):                         # contract in Phase 5, real in Phase 7
    def request(self, request: ApprovalRequest) -> Approval: ...
    def decide(self, approval_id: UUID, decision: Decision, *, by: AuthContext,
               shown_hash: str) -> Approval: ...
    def status(self, approval_id: UUID) -> Approval: ...

class CircuitBreakers(Protocol):                         # PyBreaker, state shared in Postgres
    def guard(self, key: str) -> AbstractAsyncContextManager[None]: ...

class RateLimiter(Protocol):                             # aiolimiter + Postgres token bucket
    async def acquire(self, key: str, *, cost: int = 1, wait: timedelta) -> bool: ...

class ConcurrencyLimiter(Protocol):
    def slot(self, key: str, *, limit: int) -> AbstractAsyncContextManager[bool]: ...
```

## 4. Gateway algorithm

1. **Validate.** Load the `ActionDefinition` and validate `input` against its model. If the action or its version is unknown, the result is `DENIED`.
2. **Authorize.**
   - Users go through `decide` (Phase 2 and Phase 7 policy engine).
   - Employees go through `decide_tool_access` plus the policy engine, with the originating user's shop access as an upper bound.
   - Automations act under their service identity and the automation's `permissions` section.
   - Advisors are always `DENY`.
3. **Apply the approval rule.** The approval policy applies, along with ADR-0014's autonomy rule and ADR-0022's owner-trigger rule:
   - a W-policy action without `approval_id` (or `owner_trigger` for the Owner's first run) creates an approval and returns `PENDING_APPROVAL`;
   - the waiting automation execution moves to a waiting step, or the employee session is interrupted;
   - on `approvals.decided.v1` the same request is resubmitted with `approval_id`, and every check from step 2 runs again at execution time.
4. **Claim idempotency.** Insert `action_executions (action, idempotency_key)`. On conflict, return the stored result, or `DEFERRED` while the first run is still running.
5. **Route.** The router picks a binding:
   - a missing capability (`shop_capabilities`) or scope leads to the next binding or to `DENIED (scope_missing)`;
   - a browser binding needs `READY`, its flag on and its scope recorded.
6. **Apply the guards** in this order: circuit breaker → rate limiter → concurrency slot. If any is unavailable, the result is `DEFERRED` with `retry_at`.
7. **Execute** through the adapter:
   - API writes go through the outbox (PR #55's `Deliverer` for `etsy.*` destinations);
   - browser work goes through the controller.
8. **Verify.**
   - `reread`: fetch the object again and compare it with the intended state;
   - `schema`: validate the extraction against its Pydantic model;
   - `human`: a person confirms.

   A failed verification records `FAILED (verification)` and opens an incident from Phase 6.
9. **Record.** Store the result in `action_executions`, add an `audit.audit_log` row for W-policies and personal data, add an `activity_records` row, and emit `actions.executed.v1` through the outbox.

## 5. Approval contract

One approval contract for every actor and channel. Phase 5 defines it with an in-memory implementation for tests; Phase 7 implements it on Postgres.

```python
class ApprovalRequest(BaseModel):
    action: ActionRef
    request_hash: str                # SHA-256 of the canonical ActionRequest (minus approval_id)
    requester: ActorRef
    organization_id: UUID; shop_id: UUID | None
    summary: str                     # code template, never model text
    parameters: dict[str, JsonValue] # what will be sent (diff for updates)
    impact: str; risk: RiskLevel; rollback: str
    evidence: tuple[Evidence, ...]
    expires_at: AwareDatetime
    policy: Literal["W1", "W2", "W3"]
    decided_by_roles: frozenset[Role]          # W3: {OWNER}
    requires_step_up: bool                     # W2, W3

class ApprovalState(StrEnum):
    REQUESTED = "requested"; APPROVED = "approved"; REJECTED = "rejected"
    EXPIRED = "expired"; EXECUTED = "executed"; FAILED = "failed"; CANCELLED = "cancelled"
```

Rules, from Phase 7 (AC-7.3.x):

- the requester never decides its own approval, and no employee or automation decides any approval;
- the decision stores the shown hash;
- execution re-checks everything;
- one approval executes at most once.

## 6. Secret Broker and Authenticator contracts

```python
class SecretRef(BaseModel):
    kind: Literal["etsy_password", "totp", "partner_password"]
    environment_id: UUID
    account_id: UUID | None

class InjectionGrant(BaseModel):     # opaque to callers; signed server-side
    grant_id: UUID; execution_id: UUID; environment_id: UUID
    secret_ref: SecretRef; field: str; expires_at: AwareDatetime   # ttl ≤ 60 s

class DisplayedCode(BaseModel):      # dashboard only; never logged, never cached
    code: SecretStr; period_ends_at: AwareDatetime; period_seconds: int

class ConsentRecord(BaseModel):
    text_version: str; user_id: UUID; at: AwareDatetime; ip_hash: str
```

Invariants (all tested):

- no model input, tool output, log line, error, audit row or activity record contains a secret or code;
- `SecretValue` and `SecretStr` never serialize by default;
- a grant is single use and bound to one execution;
- a code is issued for injection at most once per TOTP period.

## 7. State machines

### Browser environment (`shop_browser_environments.status`)

| From | To | When |
|---|---|---|
| `NOT_CONFIGURED` | `PROVISIONING` | Owner or Admin creates the environment (step-up, consent) |
| `PROVISIONING` | `READY` | Profile created, credentials and TOTP enrolled, preflight and the login check pass |
| `PROVISIONING` | `LOGIN_REQUIRED` | Credentials missing or login needs a person |
| `PROVISIONING` | `FAILED` | Provisioning error after its retries |
| `READY` | `LOGIN_REQUIRED` | The session expired and automatic sign-in is not possible, or a passkey or verification link is needed |
| `READY` | `DEGRADED` | A preflight or health check failed, or the error rate is above its threshold |
| `READY`, `DEGRADED`, `LOGIN_REQUIRED` | `SUSPENDED` | Kill switch, two challenges in 24 h, an Etsy warning, or a person's decision |
| `LOGIN_REQUIRED` | `READY` | A person completed the login (Human task) and the health check passes |
| `DEGRADED` | `READY` | Health checks pass again (two in a row) |
| `DEGRADED` | `FAILED` | Unrecoverable (corrupt profile, repeated crashes) |
| `SUSPENDED` | `READY` | Owner resumes after review (audited); the health check passes |
| `FAILED` | `PROVISIONING` | Owner re-provisions |
| any | `NOT_CONFIGURED` | Owner deletes the environment: profile, credentials and TOTP destroyed (audited) |

Only `READY` accepts browser actions. Every transition is recorded in `browser_environment_events` with actor, reason and correlation ID, and the database refuses any transition outside the table (trigger, as for `automation_executions`).

### Action execution (`action_executions.state`)

```text
RECEIVED ─► AUTHORIZED ─► (WAITING_APPROVAL ─► AUTHORIZED) ─► ROUTED ─► RUNNING ─► VERIFYING ─► SUCCEEDED
   │             │                │                                │           │
   └► DENIED     └► DENIED        └► REJECTED / EXPIRED            ├► DEFERRED ─► ROUTED
                                                                   ├► HANDED_TO_HUMAN ─► RUNNING (resume) | CANCELLED
                                                                   └► FAILED ─► (retry policy) ─► ROUTED | DEAD_LETTERED
VERIFYING ─► FAILED (verification) ─► incident
```

### Business task (`tasks.state`, Phase 7; existing `htco_domain.tasks.TaskState`)

Tasks keep their domain state machine (`BusinessTask`). The engine adds two waiting reasons, recorded on the task rather than as new states: `waiting_for_human` (Human channel) and `waiting_for_approval`. A task completes only through a recorded transition that references its evidence: an execution ID, an approval or a resource (AC-7.1.3).

### Automation execution and job

These are unchanged from ADR-0013 and PR #58 (`ExecutionState`), and from PR #53 (`JobState`).

## 8. Reliability controls

| Control | Tool | Where | Rule |
|---|---|---|---|
| Durable work, DLQ | Postgres job queue (ADR-0007) | All channels | `dead` jobs and `DEAD_LETTERED` executions in the admin view; audited retry |
| Idempotency | Postgres unique keys | Gateway, engine, outbox, `Idempotency-Key` | One effect per key |
| Retries inside an attempt | Tenacity | Adapters | Bounded, with jitter; never on 4xx except 429 |
| Circuit breaker | PyBreaker with shared state in Postgres | `etsy:api`, `etsy:web`, `browser:{shop}` | Opens after 5 failures in 60 s; half-open after 120 s |
| Rate limit | aiolimiter in-process plus a Postgres token bucket (Valkey optional) | `etsy:app` (one key's QPS and QPD across all shops), `browser:{shop}` | Budgets from the Developer Portal's real figures |
| Concurrency | Postgres advisory locks and slot counters | One engine per browser profile; per-shop write slot | `pg_try_advisory_lock` held for the session |
| Preflight | Browser Controller | Before every browser session | Chromium health, connectivity, the egress IP equals the configured one, JavaScript and resource health, disk |
| Session validation | Browser Controller | After start | Signed-in marker present; otherwise `LOGIN_REQUIRED` |
| Network diagnostics | Health job | Daily | Egress IP stable; DNS and TLS to etsy.com; reputation lookups for the egress IP recorded (diagnostic only, never used to rotate IPs) |
| Challenge handler | Browser Controller | Any page | CAPTCHA, passkey, block or permission page: stop, screenshot hash, Human task, environment status change; **no retry loop** |
| Prompt-injection guard | Gateway and Browser Use wrapper | Browser and email content | Page text is data: it can't add actions, change the recipe, widen scope or permissions, or extend the deadline; the action allowlist per recipe; output validated by schema |
| Verification | Gateway | Every write | Re-read or schema; a mismatch opens an incident |
| Cleanup | Controller and gateway | Always (finally) | Close the context, re-seal the profile, wipe temp files and secrets, release locks |
| Observability | structlog, Prometheus, Grafana OSS | Workers and API | Metrics per channel, action, shop: executions, latency, retries, breaker state, challenges, DLQ size |
| Secret access audit | Secret Broker | Every grant and redeem | Who, what, which execution; never the value |

## 9. Deployment

| Component | Runs | Phase |
|---|---|---|
| `api` | Gateway for user actions, approvals, webhooks | 1 → 5 |
| `agent-worker` (PR #54) | Job queue: automations, API and Internal channel jobs; employees from Phase 8 | 5 |
| `browser-worker` | Browser channel only: Chromium, Playwright, Browser Use; its own database role, storage prefix and fixed egress IP; no Etsy OAuth tokens | 8A |
| Monitoring | Prometheus and Grafana OSS (self-hosted on the browser worker's host or Grafana Cloud free tier) | 8A |

Separation: API workers never hold browser profiles; browser workers never hold Etsy OAuth tokens; only the Secret Broker opens sealed credentials, and only for a grant.
