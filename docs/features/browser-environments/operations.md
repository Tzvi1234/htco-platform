# Browser Environments — Operations

- **Configuration:** none in Phase 5. The API reads statuses with the database it already has; creation stays off (the registry's `creation_enabled` is false, and there is no route).
- **Statuses:** `select status, count(*) from app.shop_browser_environments group by 1;`
- **History of one shop:** `select kind, from_status, to_status, actor_kind, reason, correlation_id, occurred_at from app.browser_environment_events where shop_id = '<shop>' order by occurred_at;`
- **Rollback:** the migration is additive and no process writes the tables yet; leaving them empty is the rollback.
