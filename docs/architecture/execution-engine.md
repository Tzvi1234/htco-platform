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
   user action (API, Idempotency-Key)  ├──► DurableAutomationEngine.trigger ──► AutomationExecution
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

These are the shapes of the action contracts. **As built (slice 5.12, owner-approved deviation of 2026-10-23):** the application layer imports only the standard library and the domain (ER-ARCH-01, enforced by `tests/architecture/test_layer_purity.py`), so the models are frozen standard-library dataclasses in `htco_application.execution` (`gateway.py`, `contracts.py`), not Pydantic. Each action's input and output contracts are hand-written JSON Schema 2020-12 files in `packages/contracts/actions/`, checked by a small validator (`execution/schema.py`) that refuses keywords it does not apply. A Pydantic export can be added later at an adapter edge. The fields below are the contract either way.

```python
class Channel(StrEnum):
    API = "api"
    BROWSER = "browser"
    INTERNAL = "internal"
    HUMAN = "human"


class ActorKind(StrEnum):  # existing htco_domain.actors
    USER = "user"
    EMPLOYEE = "employee"
    AUTOMATION = "automation"
    ADVISOR = "advisor"
    SYSTEM = "system"


class ActionRef(BaseModel):
    name: str  # tool name, e.g. "etsy.inventory.update"
    version: int


class ActionRequest(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    action: ActionRef
    actor: ActorRef  # kind + id; from the authenticated context, never from model output
    on_behalf_of_user: (
        UUID | None
    )  # the originating user for employee sessions (narrowest access wins)
    organization_id: UUID
    shop_id: UUID | None
    idempotency_key: str  # 1..200 chars, deterministic per business effect
    correlation_id: str
    causation_id: str | None
    input: dict[str, JsonValue]  # validated against the action's input model before routing
    channel_hint: Channel | None = None  # a preference only; the router decides
    deadline: AwareDatetime | None = None
    approval_id: UUID | None = None  # set when resuming after a decided approval
    owner_trigger: bool = False  # the Owner ran it by hand (first-run rule, ADR-0022)


class Evidence(BaseModel):
    kind: Literal["api_response", "reread", "dom_snapshot_hash", "export_file", "human_note"]
    ref: str  # storage key or row ID; never secrets or raw buyer content
    sha256: str | None = None


class ActionOutcome(StrEnum):
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    DENIED = "denied"
    PENDING_APPROVAL = "pending_approval"
    HANDED_TO_HUMAN = "handed_to_human"
    DEFERRED = "deferred"  # breaker open or rate-limited; re-queued
    DISPATCHED = "dispatched"  # browser work queued for the browser worker; resumed through submit (ADR-0025 §3)


class ActionResult(BaseModel):
    model_config = ConfigDict(frozen=True)
    execution_id: UUID
    outcome: ActionOutcome
    channel: Channel | None
    output: dict[str, JsonValue] | None  # validated against the action's output model
    verified: bool  # the effect was re-read or validated
    evidence: tuple[Evidence, ...] = ()
    attempts: int
    error_code: str | None = (
        None  # stable codes: "scope_missing", "capability_blocked", "challenge", ...
    )
    cost: Decimal | None = None  # model or paid-API cost, if any


class ChannelBinding(BaseModel):
    channel: Channel
    recipe: str | None = None  # browser: recipe ID from the catalog; api: adapter method
    requires_capability: str | None = None  # e.g. "etsy.shipments.create" from shop_capabilities
    browser_scope_id: str | None = None  # e.g. "B-02" from etsy-automation-approval.md
    deterministic: bool = True  # False only for Browser Use recipes (employees only)


class ActionDefinition(BaseModel):
    tool: ToolDefinitionModel  # the registry's ToolDefinition (side effect, risk, data scope)
    input_schema: str  # JSON Schema ID
    output_schema: str
    bindings: tuple[ChannelBinding, ...]  # ordered by preference
    approval_policy: Literal["R0", "R1", "W1", "W2", "W3"]
    rate_limit_key: str | None  # e.g. "etsy:app" (shared app budget), "browser:{shop}"
    concurrency_key: str | None  # e.g. "shop:{shop_id}:writes"
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

    async def execute(
        self, request: ActionRequest, binding: ChannelBinding, lease: ExecutionLease
    ) -> ChannelOutcome: ...
    async def verify(self, request: ActionRequest, outcome: ChannelOutcome) -> Verification: ...


class ApiChannel(ChannelAdapter, Protocol): ...  # Etsy httpx adapter; writes via outbox


class InternalChannel(ChannelAdapter, Protocol): ...  # use cases


class HumanChannel(ChannelAdapter, Protocol):  # tasks and approvals (Phase 7)
    async def hand_off(self, request: ActionRequest, reason: HandoffReason) -> UUID: ...


class BrowserChannel(ChannelAdapter, Protocol):  # Phase 5: interface + FakeBrowserChannel
    async def preflight(self, environment_id: UUID) -> PreflightReport: ...


class BrowserController(Protocol):  # Phase 8A
    @asynccontextmanager
    def session(
        self, environment_id: UUID, *, purpose: str, deadline: AwareDatetime
    ) -> AsyncIterator[BrowserSession]:
        """Holds pg advisory lock (profile), unpacks the sealed profile, starts one engine
        (Playwright persistent context or CDP Chromium), runs preflight; on exit closes,
        re-seals the profile, drops secrets and releases the lock."""


class BrowserSession(Protocol):
    async def run_recipe(self, recipe: str, params: Mapping[str, JsonValue]) -> RecipeOutcome: ...
    async def extract(self, recipe: str, model: type[BaseModel]) -> BaseModel: ...
    async def inject_secret(self, grant: InjectionGrant, selector: str) -> None: ...


# --- The browser provider contract (ADR-0025 §1): seven layers and a capabilities declaration.
# The only implementation is ManagedChromiumProvider (htco_browser_worker). Business logic
# depends on BrowserController and BrowserSession only, never on a provider or on Chromium.
# A provider never sees an OAuth token, the Secret Broker, a database role or a network
# outside its NetworkProvider egress; it gets a secret only as a one-use value for one field.


class ProviderCapabilities(BaseModel):  # a claim the contract suite checks; it grants nothing
    model_config = ConfigDict(frozen=True)
    provider_id: str  # "managed-chromium"; shop_browser_environments.provider (Owner-only)
    engine: Literal["chromium"]
    engine_version: str  # the fleet's one pinned build (ADR-0025 §2)
    layers: frozenset[ProviderLayer]  # all seven for an enabled provider
    execution_engines: frozenset[Literal["playwright", "browser_use", "jev"]]
    cdp_transport: Literal["pipe"]  # --remote-debugging-pipe only; never a TCP or loopback port
    renew: bool
    recover: bool
    downloads: bool
    parallel_profiles: int  # sessions per worker, one per profile, each under its own lock


class ProfileProvisioning(Protocol):  # the profile manager
    async def create_profile(
        self, environment: BrowserEnvironment, config: BrowserConfiguration
    ) -> ProfileRecord: ...
    async def reprovision(self, environment: BrowserEnvironment) -> ProfileRecord: ...
    async def destroy_profile(self, environment: BrowserEnvironment) -> None: ...
    async def validate(self, config: BrowserConfiguration) -> tuple[ConfigurationIssue, ...]: ...


class SessionLifecycle(Protocol):  # status changes go through ShopEnvironmentRegistry.transition
    async def open(
        self,
        environment: BrowserEnvironment,
        *,
        config: BrowserConfiguration,
        egress: EgressProfile,
        deadline: AwareDatetime,
        fence: ProfileFence,
    ) -> ProviderHandle: ...
    async def save_session(self, handle: ProviderHandle, *, fence: ProfileFence) -> None:
        """Re-seal the profile under the session's own lock lease (ProfileFence): checked on
        that lock's own connection, never looked up by environment; the archive is written on
        that connection or with a generation compare-and-set. A lost lock never writes back."""

    async def renew(self, handle: ProviderHandle, *, fence: ProfileFence) -> HealthReport:
        """A short scheduled session: check the sign-in, re-seal refreshed cookies."""

    async def recover(
        self, environment: BrowserEnvironment, *, fence: ProfileFence
    ) -> HealthReport: ...  # restore the last good archive, then a health check
    async def close(self, handle: ProviderHandle) -> None:  # always; wipes the workspace
        ...


class EngineConnection(Protocol):
    async def connect(self, handle: ProviderHandle) -> PipeConnection:
        """The session's CDP pipe, in the browser worker's process only: no port, no URL. Playwright
        and Browser Use drive this object in process; no other session can reach it."""


class EngineExecution(Protocol):  # the engines that act on a page (ADR-0025 §5)
    async def run_steps(
        self, handle: ProviderHandle, recipe: RecipeSpec, params: Mapping[str, JsonValue]
    ) -> RecipeOutcome: ...  # Playwright: deterministic, the only engine automations use
    async def run_agent(
        self, handle: ProviderHandle, task: AgentTask, *, gateway: RunToken
    ) -> AgentOutcome: ...  # Browser Use, employees only, model calls through the gateway
    async def decide(
        self, handle: ProviderHandle, question: StepQuestion, *, gateway: RunToken
    ) -> StepAnswer: ...  # Jev, only for families whose 9.6 benchmark passed


class SessionAuthentication(Protocol):  # the session redeems grants; the provider never does
    async def signed_in(self, handle: ProviderHandle, site: str) -> bool: ...
    async def challenge(self, handle: ProviderHandle) -> ChallengeKind | None: ...
    async def type_secret(
        self, handle: ProviderHandle, value: SecretValue, field: FieldRef
    ) -> None: ...


class ProviderNetworking(Protocol):
    async def observed_egress(self, handle: ProviderHandle) -> str:
        """The public IP that HTCO's echo endpoint sees from inside the session."""


class ProviderMonitoring(Protocol):
    async def health_check(self, handle: ProviderHandle) -> HealthReport: ...
    async def engine_version(self) -> str: ...


class BrowserProvider(
    ProfileProvisioning,
    SessionLifecycle,
    EngineConnection,
    EngineExecution,
    SessionAuthentication,
    ProviderNetworking,
    ProviderMonitoring,
    Protocol,
):
    capabilities: ProviderCapabilities


class LiveSession(Protocol):  # ADR-0026 §3, in the browser worker only, on the session's pipe
    async def start_screencast(self, limits: FrameLimits) -> AsyncIterator[Frame]: ...
    async def stop_screencast(self) -> None: ...
    async def pause_frames(self) -> None: ...  # before a secret is typed; drops the cached frame
    async def resume_frames(self) -> None: ...  # after a navigation or a verified mask only
    async def dispatch(self, event: ControllerInput, *, lease: ControlLease) -> None:
        """The closed set only (mouse, wheel, keys, insertText), through the one input gate:
        refused while the agent runs, or for anyone but the lease holder."""


class BrowserTakeover(Protocol):  # ADR-0026 §2: one leased Controller per session
    async def request_control(
        self, session_id: UUID, *, by: AuthContext, purpose: TakeoverPurpose
    ) -> ControlLease: ...  # CONTROL_BROWSER_SESSION, aal2, step-up; compare-and-set
    async def renew(self, lease: ControlLease) -> ControlLease: ...  # the holder only
    async def release(self, lease: ControlLease) -> ReturnChecks: ...  # re-checks, then resume
    async def state(self, session_id: UUID) -> ControlState: ...


class BrowserConfigurations(Protocol):  # ADR-0025 §2: one versioned profile per environment
    async def current(self, environment_id: UUID) -> BrowserConfiguration: ...
    async def change(
        self, environment_id: UUID, change: ConfigurationChange, *, by: AuthContext
    ) -> ConfigurationVersion | tuple[ConfigurationIssue, ...]:
        """Owner or Admin with step-up, audited; validated before it is saved. Locale, language
        and the egress assignment only: no User-Agent, screen size, time zone or geolocation."""


class NetworkProvider(Protocol):  # ADR-0025 §4; first adapter: SharedEgress
    async def egress_for(self, shop_id: UUID) -> EgressProfile:
        """shared (default, no allocation needed) or dedicated (an HTCO-owned static IP)."""

    async def allocate(self, shop_id: UUID, *, by: ActorRef) -> EgressProfile: ...  # Owner, audited
    async def release(self, shop_id: UUID, *, by: ActorRef) -> None: ...  # Owner, audited
    async def check(self, shop_id: UUID) -> EgressCheck:
        """The public IP seen by HTCO's echo endpoint equals the expected egress."""


# --- Browser dispatch (ADR-0025 §3, amends ADR-0019): the Browser channel never awaits a browser.


class BrowserDispatch(Protocol):  # the agent-worker side of the Browser channel
    async def dispatch(
        self, request: ActionRequest, binding: ChannelBinding, *, execution_id: UUID, attempt: int
    ) -> UUID:
        """In the claim's transaction: the execution moves to DISPATCHED with its routed binding
        and the recipe's declared parameters stored on the claim, one browser.run job (key:
        execution and attempt; concurrency: the profile), the deadline watchdog and
        actions.dispatched.v1. The job is a locator only, the execution ID and the attempt:
        any other field is refused, and the browser worker loads the binding from the claim."""

    async def outcome_of(self, execution_id: UUID, attempt: int) -> BrowserRunOutcome | None: ...


class BrowserRunOutcome(BaseModel):  # one row per execution and attempt: started, then final once
    model_config = ConfigDict(frozen=True)
    execution_id: UUID
    attempt: int
    # started, then once: succeeded, failed, challenge, refused, timeout or interrupted
    status: RunStatus
    # The started marker: committed before an approval-gated run or a write touches a page.
    started_at: AwareDatetime | None
    # Validated against the recipe's schema; DC1 only where the recipe's output declares it.
    output: dict[str, JsonValue] | None
    evidence: tuple[Evidence, ...] = ()
    # "kill_switch", "flag_off", "not_ready", "deadline", "approval_revoked", ...
    error_code: str | None = None


class ShopEnvironmentRegistry(Protocol):
    def get(self, shop_id: UUID) -> BrowserEnvironment | None: ...
    def create(
        self, shop_id: UUID, *, by: ActorRef, consent: ConsentRecord
    ) -> BrowserEnvironment: ...
    def transition(
        self,
        environment_id: UUID,
        target: EnvironmentStatus,
        *,
        reason: str,
        by: ActorRef,
        expected: EnvironmentStatus,
    ) -> BrowserEnvironment: ...
    def record_health(self, environment_id: UUID, report: HealthReport) -> None: ...


class EnvironmentProvisioner(Protocol):  # contract in Phase 5, real in 8A
    async def provision(self, environment_id: UUID) -> ProvisioningResult: ...
    async def deprovision(self, environment_id: UUID) -> None: ...


class SecretBroker(Protocol):  # contract in Phase 5, real in 8B
    def issue_injection_grant(
        self,
        *,
        execution_id: UUID,
        environment_id: UUID,
        secret_ref: SecretRef,
        field: str,
        ttl: timedelta,
    ) -> InjectionGrant: ...
    def redeem(self, grant: InjectionGrant, *, worker: ActorRef) -> SecretValue:
        """Browser workers only; one use; audited; SecretValue has no __repr__/__str__."""


class Authenticator(Protocol):  # Phase 8B
    def enrol(
        self,
        *,
        shop_id: UUID,
        label: str,
        secret_or_uri: SecretValue,
        confirm_code: str,
        by: ActorRef,
        consent: ConsentRecord,
    ) -> TotpAccount: ...
    def rotate(
        self, account_id: UUID, *, new_secret: SecretValue, confirm_code: str, by: ActorRef
    ) -> TotpAccount: ...
    def remove(self, account_id: UUID, *, by: ActorRef) -> None: ...
    def current_code_for_display(
        self, account_id: UUID, *, viewer: AuthContext
    ) -> DisplayedCode: ...
    def code_for_injection(self, grant: InjectionGrant) -> SecretValue: ...


class ApprovalService(Protocol):  # contract in Phase 5, real in Phase 7
    def request(self, request: ApprovalRequest) -> Approval: ...
    def decide(
        self, approval_id: UUID, decision: Decision, *, by: AuthContext, shown_hash: str
    ) -> Approval: ...
    def status(self, approval_id: UUID) -> Approval: ...


class CircuitBreakers(Protocol):  # PyBreaker, state shared in Postgres
    def guard(self, key: str) -> AbstractAsyncContextManager[None]: ...


class RateLimiter(Protocol):  # aiolimiter + Postgres token bucket
    async def acquire(self, key: str, *, cost: int = 1, wait: timedelta) -> bool: ...


class ConcurrencyLimiter(Protocol):
    def slot(self, key: str, *, limit: int) -> AbstractAsyncContextManager[bool]: ...
```

**As built (slice 5.13):** `ExecutionRouter.choose(definition, request)` returns a `Route` (binding and adapter) or a `NoRoute` (code and reason); the routing context is the request itself. The browser checks go through a `BrowserGate` port (environment status, flag time, scope time) that the registry of 5.16 implements. Adapters take `(request, binding)` and `verify` returns a `bool`; the `ExecutionLease` and `Verification` values arrive with the browser controller (8A). `HumanChannel.hand_off` is the gateway's `HumanTasks.hand_off(execution_id, request, reason)`, one task per execution.

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
   - browser work is dispatched: the action becomes `DISPATCHED`, the browser worker runs it through the controller, and a resubmission resumes the claim once the outcome is in ([ADR-0025](../adr/0025-browser-and-network-providers.md) §3).
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
    request_hash: str  # SHA-256 of the canonical ActionRequest (minus approval_id)
    requester: ActorRef
    organization_id: UUID
    shop_id: UUID | None
    summary: str  # code template, never model text
    parameters: dict[str, JsonValue]  # what will be sent (diff for updates)
    impact: str
    risk: RiskLevel
    rollback: str
    evidence: tuple[Evidence, ...]
    expires_at: AwareDatetime
    policy: Literal["W1", "W2", "W3"]
    decided_by_roles: frozenset[Role]  # W3: {OWNER}
    requires_step_up: bool  # W2, W3


class ApprovalState(StrEnum):
    REQUESTED = "requested"
    APPROVED = "approved"
    REJECTED = "rejected"
    EXPIRED = "expired"
    EXECUTED = "executed"
    FAILED = "failed"
    CANCELLED = "cancelled"
```

**As built (slice 5.14):** frozen dataclasses in `htco_application.approvals` (the layer rule, as in §2), the states and decision rules in `htco_domain.approvals`. `ApprovalService` also has `executed(approval_id, *, execution_id)`: the gateway calls it when the approved request succeeded, so the approval is `EXECUTED` and bound to that execution. A resubmission without `approval_id` uses the approval already requested for that very request (same hash), so a resumed automation simply submits again. An automation names its own execution as the request's `causation_id`; the approval keeps it as `waiting_execution_id`, and `ApprovalResumer` resumes that execution once on approval, or cancels it (`approval_rejected`, `approval_expired`).

**Across processes (AUD-03):**
- **The decision.** A decision or an expiry writes `approvals.decided.v1` to the outbox with the change (`decision_event`). The service calls no listener. Each decided state is its own event, keyed `approvals.decided.v1:<approval>:<state>`: an approved approval that lapses before it runs also writes `expired`, and the outbox never drops it as a duplicate.
- **The resume.** `ApprovalResumer` is the subscriber of that event on the worker's `events.publish`, so it does not matter which process decided. A replayed delivery resumes nothing twice.
- **The wait it resumes.** The approval keeps the gateway's action execution that waits (`action_execution_id`); a handler's `Waiting` names the same action, and the engine stores it as the execution's `waiting_for`. A decision resumes or ends only that wait. The engine's optimistic update checks `waiting_for` with the state and the attempts, so a replay delivered after the run reached a later wait changes nothing.
- **The worker's gate.** Until the Postgres store (7.11), the worker's gateway uses `NoApprovals`: a write is recorded as pending, and nothing in the worker can approve it.

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


class InjectionGrant(BaseModel):  # opaque to callers; signed server-side
    grant_id: UUID
    execution_id: UUID
    environment_id: UUID
    secret_ref: SecretRef
    field: str
    expires_at: AwareDatetime  # ttl ≤ 60 s


class DisplayedCode(BaseModel):  # dashboard only; never logged, never cached
    code: SecretStr
    period_ends_at: AwareDatetime
    period_seconds: int


class ConsentRecord(BaseModel):
    text_version: str
    user_id: UUID
    at: AwareDatetime
    ip_hash: str
```

**As built (slice 5.17):** standard-library dataclasses in `htco_domain.secrets` and `htco_application.secrets` (the layer rule). `SecretStr` is `SecretValue` throughout. `issue_injection_grant` also takes `by` (the asking run), and `enrol` also takes the `environment_id` its codes are injected into. The vault is a port (`SecretVault`) that `EnvelopeSealer` satisfies. PyOTP sits behind the `TotpCodes` port in `htco_integrations.totp`, because the application layer imports only the standard library. The action catalog refuses any output schema that could carry a secret or a code (`secret_carrying_fields`).

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

**As built (slice 5.16):** `htco_domain.browser` holds the table; `htco_application.browser_environments.EnvironmentRegistry` implements `ShopEnvironmentRegistry` (with a `correlation_id` on `create`, `transition` and `record_health`, and a compare-and-set on `expected`), and `EnvironmentBrowserGate` is the router's `BrowserGate`. The database repeats the table in `private.browser_environment_edge_allowed`.

Only `READY` accepts browser actions. Every transition is recorded in `browser_environment_events` with actor, reason and correlation ID, and the database refuses any transition outside the table (trigger, as for `automation_executions`).

### Action execution (`action_executions.state`)

```text
RECEIVED ─► AUTHORIZED ─► (WAITING_APPROVAL ─► AUTHORIZED) ─► ROUTED ─► RUNNING ─► VERIFYING ─► SUCCEEDED
   │             │                │                                │           │
   └► DENIED     └► DENIED        └► REJECTED / EXPIRED            ├► DEFERRED ─► ROUTED
                                                                   ├► HANDED_TO_HUMAN ─► RUNNING (resume) | CANCELLED
                                                                   └► FAILED ─► (retry policy) ─► ROUTED | DEAD_LETTERED
ROUTED ─► DISPATCHED (Browser channel, ADR-0025 §3) ─► RUNNING (resume: the outcome is in) ─► VERIFYING
              ├► HANDED_TO_HUMAN (a challenge, or a write that started with no outcome: interrupted by the watchdog)
              ├► CANCELLED (its approval was revoked or expired before the attempt started, in the same transaction)
              └► FAILED (refused at execution time, approval_revoked included, or a read past its deadline)
VERIFYING ─► FAILED (verification) ─► incident
```

A `DISPATCHED` action holds no lease, slot or connection while the browser worker runs it. A deadline watchdog (`browser.resume` at the deadline plus grace), enqueued at dispatch, makes sure it never waits for ever. A resubmission with the same key resumes the stored claim: with no outcome yet it answers `DISPATCHED`, and with one it validates and verifies the output and records one final result. The browser step never runs twice.

### Browser session control (`browser_sessions.control_state`, ADR-0026)

| From | To | When |
|---|---|---|
| `AGENT` | `PAUSING` | A Controller's lease is granted (Take Control). The bounded deadline extension is recorded on the run's stored claim in the same transaction |
| `PAUSING` | `HUMAN_CONTROLLING` | The agent or automation stopped at its safe point. From now on, the input gate takes only the Controller's events |
| `HUMAN_CONTROLLING` | `RETURNING` | The lease holder returns control. An expired lease never causes this |
| `RETURNING` | `AGENT` | Every return check passed, and the same attempt resumes from its checkpoint |
| `PAUSING`, `HUMAN_CONTROLLING`, `RETURNING` | *(the session ends)* | The run ended before its safe point; a return check failed (`HANDED_TO_HUMAN` or `FAILED`); the maximum duration passed (`HANDED_TO_HUMAN`); or the worker crashed, and the started marker and the watchdog resolve the attempt |

While `PAUSING`, the agent finishes its current step and the input gate still refuses the Controller. While `RETURNING`, it refuses both sides. An expired lease changes nothing here: the session stays `HUMAN_CONTROLLING`, with no holder, until a Controller takes control again or the maximum duration ends it. Throughout, the action stays `DISPATCHED`. The status layer reports one more waiting reason for it, `waiting_for_human_control`, in place of `waiting_for_browser` while the session is `HUMAN_CONTROLLING`. The database refuses any transition outside the table (a trigger), as for the environment's status.

### Business task (`tasks.state`, Phase 7; existing `htco_domain.tasks.TaskState`)

Tasks keep their domain state machine (`BusinessTask`). The engine adds two waiting reasons, recorded on the task rather than as new states: `waiting_for_human` (Human channel) and `waiting_for_approval`. A task completes only through a recorded transition that references its evidence: an execution ID, an approval or a resource (AC-7.1.3).

### Status management (slice 5.18, as built)

- **One vocabulary.** `htco_domain.status` maps every state of `ExecutionState`, `ActionState`, `EnvironmentStatus` and `TaskState` to one `StatusKind` (`active`, `waiting`, `succeeded`, `failed`, `stopped`), and names the waiting reasons (`waiting_for_human`, `waiting_for_approval`, and `waiting_for_browser` for a `DISPATCHED` action, ADR-0025 §3).
- **One event per status change**, through the outbox in the same transaction:
  - an automation execution's change is `automations.execution_state_changed.v1`, written by the execution store with the change row;
  - an action's recorded step is `actions.executed.v1` (a result), `actions.approval_required.v1`, `actions.handed_to_human.v1`, `actions.deferred.v1` or `actions.dispatched.v1` (ADR-0025 §3); a resubmission that changes nothing publishes nothing;
  - a browser environment's change is `browser.environment_status_changed.v1` (5.16).
- **The chain view.** `GET /v1/executions/{id}` lists the trigger (the inbox event when the execution's `causation_id` names it), the state changes, the actions the execution caused (`causation_id` = its ID) or that share its correlation ID, with channel and outcome, their approvals and hand-offs, and the published events, in causal order.

### Automation execution and job

These are unchanged from ADR-0013 and PR #58 (`ExecutionState`), and from PR #53 (`JobState`).

## 8. Reliability controls

| Control | Tool | Where | Rule |
|---|---|---|---|
| Durable work, DLQ | Postgres job queue (ADR-0007) | All channels | `dead` jobs and `DEAD_LETTERED` executions in the admin view; audited retry |
| Idempotency | Postgres unique keys | Gateway, engine, outbox, `Idempotency-Key` | One effect per key |
| Retries inside an attempt | `InlineRetry` in the gateway (slice 5.15), the only retry layer: the worker builds the API channel's Etsy adapter with one attempt and no `Retry-After` wait (`gateway_etsy_config`, audit P1-2) | Every channel call | At most 3 HTTP calls per action attempt (`tests/worker/test_gateway_retries.py`); 5xx and lost connections retried for reads only (a write is never resent blindly); a 429 waits for a short `Retry-After` (up to 5 s) or defers; any other 4xx never retried |
| Circuit breaker | `htco_domain.guards` rules, state in `jobs.circuit_breakers` (slice 5.15) | `etsy:api`, `etsy:web`, `browser:{shop}` (the binding's `breaker_key`) | Opens after 5 failures in 60 s; half-open after 120 s with one probe at a time; 4xx and 429 are not failures |
| Rate limit | Aligned-window counters in `jobs.rate_windows` (slice 5.15; Valkey optional later behind the `RateLimiter` port) | `etsy:app` (one key's QPS and QPD across all shops), `browser:{shop}` | `[[rate_limits]]` in `registry/actions/`; placeholders until the Developer Portal's real figures |
| Concurrency | Leased slots in `jobs.concurrency_slots` (slice 5.15) | One engine per browser profile; per-shop write slot (`concurrency_key`) | A lease of the action's timeout plus 60 s; an expired lease is taken over, so no pooled connection is held during the call |
| Preflight | Browser Controller | Before every browser session | Chromium health, connectivity, the egress IP equals the configured one, JavaScript and resource health, disk |
| Session validation | Browser Controller | After start | Signed-in marker present; otherwise `LOGIN_REQUIRED` |
| Network diagnostics | Health job | Daily | Egress IP stable; DNS and TLS to etsy.com; reputation lookups for the egress IP recorded (diagnostic only, never used to rotate IPs) |
| Challenge handler | Browser Controller | Any page | CAPTCHA, passkey, block or permission page: stop, screenshot hash, Human task, environment status change; **no retry loop** |
| Prompt-injection guard | Gateway and Browser Use wrapper | Browser and email content | Page text is data: it can't add actions, change the recipe, widen scope or permissions, or extend the deadline; the action allowlist per recipe; output validated by schema |
| Verification | Gateway | Every write | Re-read or schema; a mismatch opens an incident |
| Cleanup | Controller and gateway | Always (finally) | Close the context, re-seal the profile, wipe temp files and secrets, release locks |
| Observability | structlog, Prometheus, Grafana OSS | Workers and API | Metrics per channel, action, shop: executions, latency, retries, breaker state, challenges, DLQ size |
| Secret access audit | Secret Broker | Every grant and redeem | Who, what, which execution; never the value |

**As built (slice 5.15, deviation from the plan's tools, recorded 2026-10-23):** the application layer imports only the standard library and the domain (ER-ARCH-01). So the breaker, the rate limiter and the inline retries are small rules in `htco_domain.guards` and `htco_application.execution.guards` rather than PyBreaker, aiolimiter and Tenacity. Their shared state is in Postgres (`htco_persistence.guards`), which is where the plan put it anyway. Advisory locks became leased slot rows, so a call never pins a pooled connection. An in-process aiolimiter could be added later behind the `RateLimiter` port, but it is not needed for correctness: the Postgres windows are the authority.

## 9. Deployment

| Component | Runs | Phase |
|---|---|---|
| `api` | Gateway for user actions, approvals, webhooks | 1 → 5 |
| `agent-worker` (PR #54) | Job queue: automations, API and Internal channel jobs; employees from Phase 8 | 5 |
| `browser-worker` | Browser channel only: `ManagedChromiumProvider` (the one pinned Chromium, Playwright, Browser Use). It claims only `browser.*` jobs, re-checks the claim and the switches before acting, and publishes each run's outcome (ADR-0025 §3). Its own database login (`htco_browser`), its egress from `NetworkProvider`, no Etsy OAuth tokens and no provider keys | 8A |
| Monitoring | Prometheus and Grafana OSS (self-hosted on the browser worker's host or Grafana Cloud free tier) | 8A |

Separation: API workers never hold browser profiles; browser workers never hold Etsy OAuth tokens; only the Secret Broker opens sealed credentials, and only for a grant.
