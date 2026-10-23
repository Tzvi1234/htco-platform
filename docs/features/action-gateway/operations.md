# Action Gateway — Operations

- **Contracts:** add or change an action in `registry/actions/*.toml` and its schemas in `packages/contracts/actions/`; `uv run just registry` validates them.
- **State:** `select action_name, state, outcome, error_code, count(*) from app.action_executions group by 1, 2, 3, 4;`
- **Waiting actions:** `state in ('waiting_approval', 'handed_to_human')` (index `action_executions_waiting_idx`).
- **Rollback:** the migrations are additive; the deployed API and worker still use `UnavailableGateway`, so nothing calls the gateway until it is wired (5.14 and 5.11).
- **Hand-offs:** `select action_name, reason from app.action_executions where state = 'handed_to_human';` (`security_challenge` or `human_channel`).
- **Breakers:** `select key, state, failures, opened_at, probe_at from jobs.circuit_breakers;` An open breaker closes by itself after one good probe. To close one by hand, run `update jobs.circuit_breakers set state = 'closed', failures = 0, opened_at = null, probe_at = null where key = 'etsy:api';` (audited operator action).
- **Budget use today:** `select key, used from jobs.rate_windows where scope = 'day' and starts_at = date_trunc('day', now() at time zone 'utc') at time zone 'utc';`
- **Slots:** `select key, holder, lease_until from jobs.concurrency_slots;` A slot whose lease has passed is taken over by the next write.
- **Deferred actions:** `select action_name, error_code, retry_at from app.action_executions where state = 'deferred';`
