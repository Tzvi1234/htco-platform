# ADR-0014: A central tool registry with capability-level, least-privilege access

- **Status:** Accepted; amended 2026-10-23 by [ADR-0019](0019-unified-execution-engine-and-action-gateway.md) (every tool call of every actor passes the Action Gateway)
- **Date:** 2026-10-14
- **Deciders:** Owner, engineering
- **Related:** [ADR-0012](0012-living-office-organization.md), [PERMISSION_MATRIX §2](../architecture/PERMISSION_MATRIX.md)

## Context

Employees act only through tools. The first plan described tools as typed capabilities checked by the policy engine (FR-AGT-03, FR-AGT-04) but kept no registry: who owns a tool, which version is live, its risk and side effects, and who may call it were implicit in code.

## Decision drivers

- Least privilege per employee; read-only and mutating access granted separately.
- The backend decides, whatever a model asks for (Scenario H).
- Tools are products: versioned, owned, tested, documented and measured.
- Untrusted content (web pages, emails, supplier data) never changes permissions or instructions.

## Options considered

1. **Tools as code only, permissions in prompts.** Simple, unsafe: a prompt is not an access control.
2. **A registry of tool definitions plus per-employee grants, enforced by the backend.**
3. **A generic MCP gateway exposing everything with OAuth scopes.** Useful later for external clients, too coarse for per-employee business risk today.

## Decision

Option 2.

- **Definitions** (`ToolDefinition`): name (`family.action`), version, family, owner (an employee accountable for it, usually Iris, Alex or Amir), side effect (`none`, `internal`, `external`, `financial`), risk (`low` to `critical`), data scope (data class), audit requirement, readiness and description. External and financial tools must be audited and at least medium risk. Input and output contracts, rate limits, cost controls, failure behaviour and fixtures are added per tool when it is built.
- **Families**: Office & CRM (tasks, approvals, incidents, activity, messages, proposals), Commerce & Catalog, Supplier Research, Research & Information Retrieval, Finance & Pricing, Creative & Media, Communication & Email, GitHub & Development, Infrastructure & Observability, Decision Advisor, Model Gateway, Automation Execution. 40 initial tools are in `registry/tools/`.
- **Access** (`decide_tool_access`): deny an unknown employee or tool, an employee that is not production-ready, a tool not in the employee's grants, or a tool that is not ready; require approval for critical tools always, and for external or financial tools below autonomy L3; otherwise allow. The decision is recorded as an activity record. A Decision Advisor's answer never enters this function.
- **Automations as tools**: an employee reaches an automation only through `automation.trigger`, and only for automations in its `allowed_automations` that are callable from `agent_tool` (the registry refuses other grants).
- **No credentials in tools' callers**: tools call use cases under the employee's service identity; tenant and acting user come from the authenticated run, never from model output.

## Consequences

- Positive: every tool call is explainable (who, which version, why allowed); access reviews are diffs of `registry/`.
- Negative: each new tool needs a definition, an owner and tests before any employee can use it.
- Follow-ups: tool invocation records and rate limits (Phase 8); the AI Tools Center in the office UI (Phase 15).

## Validation

Scenario H passes now for tools and automations (`tests/application/test_tool_access.py`, `test_automation_engine.py`); it is repeated end to end through the agent runtime's tool bridge in Phase 8.

## Validation log

- 2026-10-14 — Pure access rules tested; the registry validates grants against definitions.
- 2026-10-23 — Amended by ADR-0019 and ADR-0022: the registry holds 81 draft tools, including 38 Etsy Open API v3 tools (`registry/tools/etsy_open_api.toml`) and two browser tools; `decide_tool_access` runs inside the Action Gateway for employees, automations and users alike. No tool returns a secret or a one-time code (ADR-0021).
