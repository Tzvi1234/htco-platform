# ADR-0011: psycopg with explicit SQL; Supabase CLI migrations as the single schema source

- **Status:** Accepted
- **Date:** 2026-10-14
- **Deciders:** Owner, engineering

## Context

The schema relies heavily on Postgres features an ORM handles poorly: row-level security policies, grants, security-definer functions in a private schema, `SKIP LOCKED` queues, `NUMERIC` money, and the LangGraph checkpoint tables. Two schema sources (for example Alembic and Supabase migrations) would drift.

## Decision

- **Driver:** psycopg 3 with `psycopg_pool`, the same driver LangGraph's checkpointer uses.
- **Queries:** explicit, parameterized SQL inside repository adapters in `htco_persistence`, mapped to domain types. SQLAlchemy 2.1 Core may be used to compose dynamic queries; no ORM models.
- **Schema source:** SQL migrations in `supabase/migrations`, created and applied with the Supabase CLI, tested with pgTAP, applied to production only from CI after the owner starts the release. No Alembic.
- **RLS in user requests:** the API opens a transaction, sets `role authenticated` and the verified claims with `set local`, then runs queries, so RLS applies even if code forgets a filter. The request role never has `BYPASSRLS`, and the Supabase secret key is never used on request paths.
- **LangGraph tables** are created by a migration (not by `setup()` at runtime) in the `agent_runtime` schema, which the Data API does not expose.

## Consequences

- Positive: one schema truth, full use of Postgres, reviewable SQL, RLS as defense in depth.
- Negative: more hand-written SQL and mapping code; repository tests against a real local database are mandatory.

## Validation

Phase 2: pgTAP tests prove RLS on every table with two tenants; an API test proves that a query missing its tenant filter still cannot read another tenant's rows.
