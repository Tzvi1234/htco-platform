# Contracts

Language-neutral contracts of the HTCO platform. The clients in `htco-clients` pin a copy and generate their API clients from it.

| File | What it is | How it changes |
|---|---|---|
| [`openapi.yaml`](openapi.yaml) | The HTTP API contract (OpenAPI 3.1) | Generated from the API with `uv run just openapi`, reviewed in the pull request. CI fails when it differs from the API, and `oasdiff` fails on breaking changes against the base branch. |

Event schemas join this folder with the first events (Phase 4).
