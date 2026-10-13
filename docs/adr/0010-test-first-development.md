# ADR-0010: Test-first development with mechanical enforcement

- **Status:** Accepted
- **Date:** 2026-10-14
- **Deciders:** Owner, engineering

## Context

Most HTCO code will be written by AI coding agents under the owner's direction. Agents are fast, but they can drift: skipping tests, weakening assertions to make a build pass, or declaring work done without running it. Written instructions (CLAUDE.md) are advisory; hooks and CI are deterministic.

## Decision

1. **Test-first is mandatory** for production code: acceptance criteria, then a failing test, then code ([TESTING_STRATEGY.md](../engineering/TESTING_STRATEGY.md)).
2. **Skills describe the procedure**: `tdd` for the loop and edge cases, `feature-verification` for verifying each feature type for real, `feature-docs` for recording evidence.
3. **Mechanical enforcement**, added in Phase 1 and extended per phase:
   - CI runs format, lint, strict type checks, architecture contracts, unit, integration, contract and database tests, and security scans on every pull request.
   - A CI check flags test weakening (new skip or expected-failure markers, deleted tests, lowered thresholds, changed expected values) unless the pull request explains it under "Test changes".
   - Claude Code project hooks run fast checks after edits and before an agent declares a task finished.
   - Coverage floors per layer, a mutation score that must not drop on money and authorization code, and accessibility gates on the web.
4. **Independent review**: a fresh-context reviewer (a second agent session or the owner) reviews every pull request against the checklist before merge.

## Consequences

- Positive: a safety net that does not depend on any one session's diligence; the test suite becomes living documentation of behaviour.
- Negative: slower first commits; CI time grows and needs care (path filters, caching).

## Validation

Track per phase: escaped defects (bugs found after merge), flaky-test count, and the share of pull requests whose first commit is a failing test.
