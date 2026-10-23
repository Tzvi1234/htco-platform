# Action Gateway — Operations

- **Contracts:** add or change an action in `registry/actions/*.toml` and its schemas in `packages/contracts/actions/`; `uv run just registry` validates them.
- **State:** `select action_name, state, outcome, error_code, count(*) from app.action_executions group by 1, 2, 3, 4;`
- **Waiting actions:** `state in ('waiting_approval', 'handed_to_human')` (index `action_executions_waiting_idx`).
- **Rollback:** the migration is additive; the deployed API and worker still use `UnavailableGateway`, so nothing calls the gateway until 5.13 wires it.
