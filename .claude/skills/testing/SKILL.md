---
name: testing
description: How to write correct, trustworthy tests in htco-platform — choosing the test level for each layer, fakes versus mocks, test data, the exact patterns for domain, use case, persistence, adapter, API, worker, webhook and Claude runtime tests, how to prove a test can fail, and the anti-patterns that make tests lie. Load together with `tdd` whenever you write or review a test, and before reviewing someone else's tests.
---

# Writing correct tests in htco-platform

`tdd` says **when** to write tests (first) and the red, green, refactor loop. This skill says **how** to write a test that actually proves something. References: [TESTING_STRATEGY.md](../../../docs/engineering/TESTING_STRATEGY.md), [CODE_DESIGN §11](../../../docs/architecture/CODE_DESIGN.md#11-where-tests-go), [ER §9](../../../docs/engineering/ENGINEERING_RULES.md#9-testing-er-test).

## 1. What a correct test is

A test is correct when **all** of these hold:

1. **It fails when the behaviour is broken.** You saw it fail for the right reason (an assertion about the behaviour, not an import error).
2. **It passes only because the behaviour works,** not because of a mock that returns what the assertion expects.
3. **It tests behaviour through a public interface:** a domain method, a use case, a port, an endpoint. A refactor that keeps the behaviour keeps the test green.
4. **It is deterministic.** No wall clock, no real randomness, no network, no ordering luck, no sleeps.
5. **It is isolated.** It creates its own data and leaves nothing behind; it passes alone and in any order.
6. **It reads as a specification.** Its name states the scenario and the expected outcome; Arrange, Act, Assert are visible; one behaviour per test.
7. **It is at the right level** (§2): the lowest level that can prove the behaviour, plus one test where the behaviour is exposed.

## 2. Pick the level from the layer

| You changed… | Write | Doubles | Folder |
|---|---|---|---|
| A domain rule, value object, state machine, money math | Unit tests plus Hypothesis property tests | None | `tests/domain/<ctx>/` |
| A use case | Application test that calls the use case | In-memory fakes of its ports (`tests/fakes/`) | `tests/application/<ctx>/` |
| A repository, outbox, inbox, queue, migration | Integration test against the local Supabase Postgres, in a rolled-back transaction | None: real Postgres | `tests/persistence/<ctx>/` |
| RLS, grants, constraints | pgTAP with two organizations | basejump helpers | `supabase/tests/` |
| An Etsy, AutoVA, email, GitHub or model adapter | Contract test against recorded, scrubbed fixtures (respx) for every documented error | Recorded HTTP | `tests/integrations/<provider>/`, `tests/model_router/` |
| An endpoint | API test through `httpx.AsyncClient(transport=ASGITransport(app))` with AnyIO | Fakes at the port level | `tests/api/` |
| A webhook | Raw-body tests: valid, bad signature, expired timestamp, replay, duplicate | — | `tests/api/` |
| A worker or job handler | Duplicate message, crash before and after commit, lease expiry, poison message to dead letter | Real Postgres | `tests/workers/` |
| The Claude runtime adapter | Scripted SDK message streams through the SDK's transport seam; option checks | No network, no real model | `tests/agent_runtime/claude/` |
| An employee's quality | Evaluation dataset and thresholds (not unit tests) | Real models, nightly, never in PR CI | eval harness (Phase 6) |

If you are writing an API test to check a pricing rule, you are at the wrong level: test the rule in the domain and test only the wiring through the API.

## 3. Fakes, stubs and mocks

- **Fakes first.** Every port has an in-memory fake that behaves like the real thing: same typed errors, same idempotency, same uniqueness. Tests assert on **outcomes** (what the fake repository now holds, which events were emitted, which outbox rows exist).
- **Mocks only to prove an interaction that is itself the behaviour** ("exactly one email requested"). Prefer a recording fake over `unittest.mock`.
- **Never mock what you do not own.** Wrap SDKs and HTTP APIs in adapters; contract-test the adapter with fixtures; fake the port above it.
- **Never mock the database in persistence tests,** and never mock the domain.
- **Never patch internals** (`patch("module._helper")`). If you need to, the design is missing a port.

## 4. Patterns

### Domain: examples plus properties

```python
def test_order__ship_without_evidence__is_refused() -> None:
    order = an_order().paid().build()

    with pytest.raises(IllegalTransition):
        order.mark_shipped(evidence=None)


@given(lines=st.lists(order_lines(), min_size=1, max_size=20))
def test_order_total__equals_sum_of_lines(lines: list[OrderLine]) -> None:
    order = an_order().with_lines(lines).build()

    assert order.total == sum_money(line.total for line in lines)
```

State machines: one test per allowed transition and at least one illegal transition from every state (parametrize over the table).

### Use case: fakes and outcomes

```python
def test_ingest_receipt__same_event_twice__creates_one_order() -> None:
    deps = FakeDeps.with_receipt(a_receipt().paid().build())
    ingest = IngestReceipt(**deps.ports())

    first = ingest(a_command(), actor=automation("etsy-order-intake"))
    second = ingest(a_command(), actor=automation("etsy-order-intake"))

    assert second == first
    assert deps.orders.count() == 1
    assert deps.outbox.count() == 1
```

Cover: happy path, each failure path, authorization denied, idempotent repeat, the events and activity records written.

### Persistence: real Postgres, rolled back

Each test runs inside a transaction that is rolled back. Assert with SQL against the real tables. For RLS, also add a pgTAP test that the second organization sees nothing.

### Adapter: every documented error

Parametrize over recorded fixtures: 200, 400, 401, 403, 404, 409, 429 with and without `Retry-After`, 5xx, timeout, malformed JSON, partial payload. Assert the typed error and that no secret or personal data reached the logs (`log_capture` fixture). Fixtures are scrubbed before commit; a test fails if a fixture contains a secret-looking string.

### API: contract and permissions

Status code, problem+json shape and `type`, 401 and 403, cross-tenant 404 (never 403, so existence does not leak), validation errors that do not echo input, pagination, `Idempotency-Key` replay. Schemathesis covers the contract broadly; your test covers the specific behaviour.

### Worker: crash where it hurts

Simulate the crash after the database commit and before the external call, and the reverse. Assert that exactly one external effect happens across retries and that an expired lease is picked up by the recovery scan.

### Claude runtime adapter (ADR-0016)

- **Options test** (must exist, never weakened): `build_options` for every registered employee has `setting_sources == []`, no built-in tool allowed, every built-in disallowed, `permission_mode` not in `{"bypassPermissions", "acceptEdits", "auto"}`, only granted `mcp__htco__*` tools, and no secret in `env` other than the runtime key.
- **Scripted streams:** feed a recorded or hand-written sequence of SDK messages (tool use requests, a `ResultMessage` with `model_usage`) through the SDK's transport seam. Assert the `StepOutcome`, the task transition, `agent_runs`, cost settlement and `tool_invocations`.
- **Bridge tests:** ungranted tool, built-in tool, other shop's ID in the arguments, approval-required tool, rate limit, timeout. Each is denied or paused and audited.
- **Durability:** kill the step mid-stream; the lease expires; the next step starts from the task brief; no tool side effect repeats.
- **Injection corpus:** pages and emails with instructions inside them change no permission, memory or instruction.

## 5. Test data

- Builders with sensible defaults: `an_order().paid().with_items(2).build()`. Override only what the test is about.
- Synthetic, obviously fake people and addresses. No production data, ever.
- Money as `Decimal("12.34")` with a currency. Time from a fixed clock (`FixedClock(at=...)`).
- Seeds always include a second organization and shop, so cross-tenant tests have a target.
- Hebrew and mixed-direction strings appear in text fields at least once per feature.

## 6. Prove the test can fail

Before you trust a new test:

1. Run it **before** the production code exists and read the failure (the `tdd` loop).
2. For a test written against existing code (a bug reproduction, a characterization test), **break the code on purpose** (flip a comparison, remove a line), watch the test fail, then restore it.
3. For money and authorization code, check the nightly mutation report from Phase 4: a surviving mutant is a missing test.

A test that cannot fail is worse than no test, because it hides the gap.

## 7. Anti-patterns that make tests lie

| Anti-pattern | Why it lies | Do instead |
|---|---|---|
| Changing the expected value to match the code | The test now specifies the bug | Find out which is wrong; explain it in the PR's "Test changes" section |
| Asserting a mock was called with what you just configured it to return | Tests the mock | Assert an outcome in a fake |
| `assert result` or `assert result is not None` | Passes for nearly anything | Assert the exact value or shape |
| Catch-all `pytest.raises(Exception)` | Passes on the wrong error | The exact typed error, and check its fields |
| Sleeps and real time | Flaky and slow | Injected clock; wait on a condition with a timeout |
| Shared mutable fixtures across tests | Order-dependent results | Build fresh data per test |
| Testing private helpers | Breaks on refactor, misses behaviour | Test through the public interface |
| Snapshot updated in CI automatically | Accepts any change | Snapshots change only in a reviewed commit |
| One test asserting ten behaviours | First failure hides the rest | One behaviour per test, parametrize variations |
| `skip`, `xfail` or deleting a failing test | Hides a real failure | Fix it, or quarantine with an issue (ER-TEST-04), never for security tests |
| Real network in CI | Flaky, leaks data, costs money | Recorded fixtures and fakes |

## 8. Review checklist for any test

- [ ] Name says unit, scenario and expected outcome; the AC ID is marked (`@pytest.mark.ac("AC-6.4.1")`).
- [ ] Right level for the layer; no business rule tested only through HTTP.
- [ ] Fakes for ports; no mocks of things we don't own; no patching internals.
- [ ] Deterministic: injected clock and IDs, no sleeps, no network.
- [ ] Asserts exact outcomes, including the failure paths and the records written (activity, audit, outbox).
- [ ] Edge cases from the `tdd` checklist considered (duplicates, concurrency, time, money, permissions, external failures, hostile input).
- [ ] It was seen failing for the right reason.
- [ ] No secrets or personal data in fixtures.
