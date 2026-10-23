# Living office — Design

## Modules

| Module | Holds |
|---|---|
| `htco_domain.actors` | `ActorKind` (`user`, `employee`, `automation`, `advisor`, `system`) and `ActorRef` |
| `htco_domain.workforce` | `Department` (7), `EmployeeIdentity`, `EmployeeProfile`, `Readiness`, `AutonomyLevel`, the 22 `EMPLOYEE_SPEC_SECTIONS`, `EvaluationResult` |
| `htco_domain.routing` | `ModelTier`, `ModelCapability`, `DataClass`, `AdvisorUsage`, `ModelRoutingPolicy` |
| `htco_domain.tooling` | `ToolFamily` (12), `SideEffect`, `RiskLevel`, `ToolDefinition` |
| `htco_domain.automation` | `TriggerKind`, the 18 `AUTOMATION_SPEC_SECTIONS`, `RetryPolicy`, `AutomationDefinition`, `AutomationExecution` and its state machine |
| `htco_domain.tasks` | `TaskState` (13) and `BusinessTask` with its transition table |
| `htco_domain.activity` | `ActivityKind`, `ActivityOutcome`, `ActivityRecord` (business summary of at most 500 characters) |
| `htco_domain.improvement` | `ProposalState` and `OptimizationProposal` |
| `htco_application.workforce.registry` | Parses registry documents into domain objects and validates cross-references, reporting every problem at once |
| `htco_application.workforce.tool_access` | `decide_tool_access` → allow, require approval, deny |
| `htco_application.automation.engine` | `TriggerRequest`, `AutomationRefused` and `authorize`; the engine is `automation.durable.DurableAutomationEngine` (the in-memory `AutomationEngine` was retired in slice 5.11) |
| `htco_application.routing.router` | `route` over `ModelOffer` catalog entries |
| `htco_application.decisions.advisor` | `DecisionAdvisor` port, `RulesAdvisor`, `advise` |
| `htco_application.agents.runtime` | `AgentRuntime` port and `AgentSession` |
| `tools/registry.py` | Loads `registry/*.toml`; `just registry` |

## The registry

```text
registry/
  employees/<id>.toml     one Agent Specification Sheet per employee (identity, grants, [routing], 22 [specification.*] sections)
  automations/<id>.toml   one specification per automation (trigger, retry, side effect, 18 sections)
  tools/<family>.toml     [[tools]] tables per family
  advisors/jev.toml       the shared Decision Advisor
```

A grant of an automation to an employee means "may trigger it through `automation.trigger`", so the registry refuses grants to automations that are not callable from `agent_tool`; ownership is the automation's `business_owner`.

## Decisions and alternatives

| Decision | Alternatives considered | Why |
|---|---|---|
| TOML documents now, database rows from Phase 8 | Database first; YAML | Reviewed in pull requests today; `tomllib` is in the standard library (no new dependency) |
| The specification sheet is the configuration file | Separate Markdown sheets | One source of truth per employee; tests check all 22 sections exist |
| Readiness enforced in the domain constructor | A check in CI only | An invalid production-ready profile cannot exist anywhere, including tests |
| Router chooses by cost among valid models | Fixed model per employee | Policies name capabilities and limits, not models (ADR-0004, ADR-0015) |
| Office & CRM as a twelfth tool family | Spreading tasks and approvals across families | Tasks, approvals, activity and messages are the office's own records |
