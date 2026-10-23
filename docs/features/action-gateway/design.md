# Action Gateway — Design

## Components

| Layer | Module | Responsibility |
|---|---|---|
| Domain | `htco_domain.actions` | `ApprovalPolicy` (R0 to W3), `VerifyMode`, `ActionState` and its machine |
| Application | `htco_application.execution.gateway` | `ActionGateway` port, `ActionRequest`, `ActionResult`, `ActionOutcome`, `Channel`, `UnavailableGateway` |
| Application | `htco_application.execution.contracts` | `ActionDefinition`, `ChannelBinding`, `ChannelOutcome`, `Evidence`, `ActionCatalog` |
| Application | `htco_application.execution.schema` | The JSON Schema subset checker |
| Application | `htco_application.execution.service` | `ActionGatewayService` and its ports: `ActionStore`, `Principals`, `ShopOwners`, `ApprovalGate`, `ChannelAdapter`, `ShopCapabilities` |
| Adapters | `htco_persistence.actions` | `PostgresActionStore` |
| Data | `registry/actions/*.toml`, `packages/contracts/actions/*.json` | The reviewed execution contracts |

## Contracts

- **Registry:** `[[actions]]` with `tool`, `approval_policy` (must equal the tool description's "Approval policy X."), `verify`, `timeout_seconds`, `input_schema`, `output_schema`, optional `rate_limit_key`, `concurrency_key`, `user_capability`, and `bindings` (`channel`, `recipe`, `requires_capability`, `browser_scope_id`, `deterministic`). A write policy needs a mutating tool.
- **Table** `app.action_executions` (`20261023150000_action_executions.sql`): unique `(organization_id, action_name, idempotency_key)`; request hash; state (the `ActionState` values); outcome; channel; attempts; verified; error code; reason; output (never for R1); evidence; actor; on-behalf-of user; correlation and causation IDs; approval ID; finished time. A trigger refuses any change to a finished row. RLS: users read their organization (Owner, Admin) or granted shops, without the output; the worker reaches its job's organization.
- **Event:** `actions.executed.v1` in `jobs.outbox` (destination `events.publish`), IDs and outcome only; the worker accepts it (consumers from Phase 6).

## Algorithm

Claim → validate → authorize → approval → route → execute → verify → record; see the module docstring of `service.py` and [execution-engine.md §4](../../architecture/execution-engine.md#4-gateway-algorithm). Authorization by actor kind: users by the policy engine and the action's `user_capability` (shops checked, the Owner alone may set `owner_trigger`); employees by `decide_tool_access` (an approval is forced when it says so); automations by `allowed_actions`; the platform for R0 only; advisors never.

## Decisions

| Decision | Why |
|---|---|
| Standard-library dataclasses plus JSON Schema files instead of Pydantic in the application layer | The layer rule (ER-ARCH-01); recorded in execution-engine.md §2 |
| Claim before validation | Every outcome, denials included, is one recorded row (AC-5.12.4) |
| Uniqueness per organization | No cross-tenant replay through a shared key |
| No stored output for R1 | Buyer data at rest only in the order store (ADR-0018) |
