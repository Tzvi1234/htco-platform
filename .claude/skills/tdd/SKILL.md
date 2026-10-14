---
name: tdd
description: Test-driven development protocol for htco-platform. Use whenever writing or changing production code, fixing a bug, or adding a migration — before the first line of implementation. Gives the red-green-refactor loop, which kind of test to write for each layer, test naming and data rules, and the edge-case checklist every slice must consider.
---

# Test-driven development in htco-platform

Reference: `docs/engineering/TESTING_STRATEGY.md`. This skill is the procedure; the [`testing`](../testing/SKILL.md) skill says how to write each kind of test correctly. Load both.

## The loop

1. **Pick one acceptance criterion** from the slice (for example `AC-4.4.1`). Write it as Given / When / Then if it is not already.
2. **Choose the test level** (table below). Prefer the lowest level that can prove the behaviour, plus one test at the boundary where it is exposed.
3. **RED:** write the test. Run only that test. Confirm it fails, and fails for the expected reason (an assertion about the behaviour, not an import error or typo). If it passes immediately, the test is wrong or the behaviour already exists: find out which.
4. **GREEN:** write the simplest production code that makes it pass. Run the test, then the package's suite.
5. **REFACTOR:** clean names, remove duplication, keep functions small. Run the full suite (`just check`).
6. **Next criterion.** When all are covered, walk the edge-case checklist below and add tests for every item that applies.
7. **Commit** the test before or together with the code. A useful pattern: `test(orders): ingest is idempotent (red)` then `feat(orders): idempotent receipt ingest`.

## Which test for which layer

| Code | Test | Doubles |
|---|---|---|
| Domain rule, value object, state machine | Unit tests + Hypothesis property tests | None |
| Use case | Application tests | In-memory fakes of ports (repositories, clock, gateway); assert outcomes and emitted events |
| Repository, outbox, queue | Integration tests against the local Supabase stack, each test in a rolled-back transaction | None — real Postgres |
| RLS, grants, constraints | pgTAP via `supabase test db`, with two tenants | basejump test helpers |
| Etsy, AutoVA, email, model adapters | Contract tests with respx and scrubbed recorded fixtures, covering every documented error | Never call real services in CI |
| Endpoint | API tests with `httpx.AsyncClient(transport=ASGITransport(app))` under the AnyIO plugin | Fakes at the port level |
| Webhook | Signature, timestamp, replay and duplicate tests on the raw body | — |
| Worker | Duplicate message, crash before and after commit, lease expiry, dead-letter | Real Postgres |
| Claude runtime adapter | Options checks; scripted SDK message streams through the transport seam; bridge denials; crash mid-step resumes from the task brief; injection fixtures | Scripted streams, fake gateway; never the network |
| LangGraph adapter (alternative) | Graph tests with a fake model gateway and scripted outputs; interrupt and resume; denied tools | `FakeModelGateway`, in-memory or Postgres saver |
| Agent quality | Evals on a gold dataset (not unit tests) | Real models, nightly |

## Naming and structure

- `test_<unit>__<scenario>__<expected>()`, for example `test_ingest_receipt__same_event_twice__creates_one_order`.
- Arrange, Act, Assert, separated by blank lines. One behaviour per test.
- Reference the acceptance criterion: `@pytest.mark.ac("AC-4.4.1")`.
- Builders for test data (`an_order().with_items(2).paid()`), never long literal dictionaries repeated across tests.
- No sleeps. Inject the clock; wait on conditions with timeouts.

## Test data rules

- Synthetic data only; obviously fake names and addresses.
- Recorded HTTP fixtures are scrubbed (tokens, emails, names, addresses) before commit.
- Seeds always include a second organization and shop so cross-tenant tests have a target.
- Money in tests uses `Decimal("12.34")`, never floats.

## Edge-case checklist (consider every line)

- [ ] Empty, missing, null, whitespace; maximum lengths; Unicode, emoji, Hebrew, mixed direction.
- [ ] Zero, one, many; first and last page; exactly at a limit and one past it.
- [ ] The same request, event or message twice — sequentially and concurrently.
- [ ] Out-of-order events; late events for closed resources.
- [ ] Two workers or users on the same resource at once.
- [ ] Time zones, DST changes, month ends, expiry exactly at "now", clock skew.
- [ ] Money rounding, currency mismatch, zero and negative amounts, refunds exceeding remaining balance.
- [ ] No session, expired session, MFA too low, wrong role, wrong shop, wrong organization.
- [ ] Timeout, 429 with and without `Retry-After`, 5xx, 403 capability missing, malformed or partial payloads.
- [ ] Crash after the database commit but before the external call, and the reverse.
- [ ] Large payloads, many items, many variants.
- [ ] Hostile input: injection strings, prompt-injection text, oversized files, wrong content types.

## Fixing a bug

1. Reproduce it with a failing test at the lowest level that shows it.
2. Find the root cause (not the symptom). Write it down in the pull request.
3. Fix, keep the test forever, and add a sibling test if the same class of bug can happen elsewhere.

## What never to do

- Change an expected value to match the code without explaining why the old expectation was wrong.
- Add `skip`, `xfail` or delete a test to get green CI (CI will flag it; the pull request needs a "Test changes" section).
- Mock the database in tests of persistence code.
- Write tests after the code and present them as test-first.
