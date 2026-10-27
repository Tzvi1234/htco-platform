# Contracts

Language-neutral contracts of the HTCO platform. The clients in `htco-clients` pin a copy and generate their API clients from it.

| File | What it is | How it changes |
|---|---|---|
| [`openapi.yaml`](openapi.yaml) | The HTTP API contract (OpenAPI 3.1) | Generated from the API with `uv run just openapi`, reviewed in the pull request. CI fails when it differs from the API, and `oasdiff` fails on breaking changes against the base branch. |
| [`events/`](events/) | One JSON Schema (2020-12) per domain event type, `<event_type>.json`: the envelope of [SOFTWARE_DESIGN §8.4](../../docs/architecture/SOFTWARE_DESIGN.md#84-event-envelope) with the event's payload | Added with the producer, and linked from [EVENT_CATALOG](../../docs/architecture/EVENT_CATALOG.md). A new or changed field is a new schema version or a new event version (catalog rule 4). The schemas use the subset that `htco_application.execution.schema` checks: a field that may be null has no `type` and says so in its `description`. `tests/contract/test_event_contracts.py` keeps the schemas, the catalog and the source in step. |
| [`actions/`](actions/) | The input and output schema of each Action Gateway action | With the action's registry entry |
| [`automations/`](automations/) | The input and output schema of each automation | With the automation's registry entry |
