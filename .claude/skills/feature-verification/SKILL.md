---
name: feature-verification
description: How to verify that a feature in htco-platform really works before calling it done — beyond the unit and edge-case tests. Use after the tests pass and before opening or merging a pull request, for every slice. Contains a universal verification protocol, a playbook per feature type (endpoint, migration and RLS, webhook, job, integration adapter, outbound side effect, state machine, money, agent graph, automation, registry change, model gateway, security change, deployment), adversarial checks, and the evidence format to record in the feature's tests.md.
---

# Feature verification

Tests prove the code does what we expected. Verification proves the feature does what the owner needs, in a running system, and that it fails safely. Do this for every slice; record the evidence; never claim a check you did not run.

## Universal protocol

1. **Outcome.** Write one sentence describing what the user or system can do now, and list the slice's acceptance criteria.
2. **Automated evidence.** Run `just check` (and `just db-test`, `just contract` when relevant). Map each acceptance criterion to the tests that cover it. A criterion without a test is not verified.
3. **Run it for real.** Start the local stack (`just up`: Supabase local, API, worker). Seed with `just seed` (two organizations, two shops, one user per role).
4. **Happy path as the user.** Exercise the feature through its real interface: HTTP calls (`httpie` or `curl` with a real session or token), the web UI for user-facing flows, or by enqueuing the real job.
5. **Try to break it.** Run the adversarial checks of the playbook below that fit the feature. Think like a careless user, a malicious tenant, a flaky network and a crashing worker.
6. **Observe.** For one happy-path run and one failure run, follow the `request_id` or `correlation_id` through logs, database rows, audit records, events, jobs and metrics. Confirm no PII or secret appears in logs.
7. **Deployed check** (when the slice deploys something): repeat the happy path and one failure path on the deployed environment, with the owner's approval for anything that touches real shop data.
8. **Record** the evidence in `docs/features/<feature>/tests.md` (format at the end), and clean up any test data you created.

## Playbooks by feature type

### API endpoint
- Call it with: no auth, expired token, `aal1` when `aal2` is required, each role in the permission matrix, a user of another organization, a user without access to the shop.
- Expect: 401, 403 or 404 exactly as designed; cross-tenant always 404 with no hint the resource exists.
- Send invalid bodies, unknown fields, huge payloads, wrong content type: problem+json with a stable `type` and no internals.
- Repeat a POST with the same `Idempotency-Key`: same response, no duplicate effect.
- Paginate to the end; check stable ordering and the maximum limit.
- Check the response matches `openapi.yaml` (Schemathesis run is green).

### Database migration and RLS
- `just db-reset` from zero, then apply on a copy of realistic seeded data.
- Run pgTAP: RLS enabled on every new table; two-tenant read and write denial; grants (no update or delete on audit tables).
- As the `authenticated` role in `psql` with claims set, try to read another tenant's rows directly. Expect zero rows.
- Run the previous application version against the new schema (expand-then-contract): it must still work.
- Check query plans of new hot queries use the tenant index (`EXPLAIN`).

### Webhook or ingest
- Valid signed event → processed once.
- Same event 10 times → one effect. Modified body → rejected. Timestamp 6 minutes old → rejected. Unknown event type → stored and ignored, logged.
- Out-of-order: newer update first, older second → newer data stays.
- Drop the webhook entirely → reconciliation recovers it within one interval.
- Response time under one second with the worker stopped (ingress must not depend on processing).

### Background job or schedule
- Enqueue the job twice with the same key → one effect.
- Kill the worker (`kill -9`) mid-job → after restart, the job completes once.
- Make the job fail every time → it retries with backoff, then lands in the dead-letter state with its error.
- Run two workers → no double processing; per-resource locks hold.
- Check the schedule enqueues exactly once per period even with two worker instances.
- Check the heartbeat and queue age metrics move.

### External integration adapter (Etsy, AutoVA, email)
- Replay every recorded fixture, including 401, 403, 404, 409, 429 (with and without `retry-after`), 5xx, timeouts and malformed JSON → each maps to the documented typed error and retry behaviour.
- Check the rate-limit budget logic with headers near the limits.
- Confirm no token, email or address appears in logs or exception messages.
- Read-only live smoke (with owner approval): one real call against the pilot shop, recorded and scrubbed as a new fixture if the response shape changed.

### Outbound side effect (email, Etsy write, notification)
- Confirm it is written to the outbox in the same transaction as the state change.
- Crash between commit and delivery → exactly one delivery after restart.
- Provider down → retries with backoff; permanent failure visible to admins.
- For non-idempotent providers (Etsy shipments): confirm the pre-write re-read skips when the effect already exists.
- Confirm required approval, role, MFA and audit record exist for the effect.

### State machine or workflow
- Drive the entity through every allowed transition; attempt each illegal one from each state.
- Confirm every transition writes an event with source and proof.
- Confirm terminal states are terminal.

### Money, pricing, finance
- Property tests: totals equal sums of lines; rounding rules applied once; no float in the path.
- Spot-check three real-shaped examples by hand (write the arithmetic in tests.md).
- Currency mismatch rejected; negative and zero amounts handled.
- Figures labeled `ESTIMATED` or `VERIFIED` correctly, with their source.

### Agent graph or capability
- Graph tests green with the fake gateway; walk one run in the local stack with the fake gateway and inspect checkpoints, tool calls, audit and costs.
- Denied tool: script the fake model to request a tool outside the allowlist → denied, audited, no effect.
- Wrong tenant: script a tool argument with another shop's ID → ignored, flagged.
- Injection: run the injection corpus fixtures through every input that carries untrusted content → no permission change, no side effect, flagged as suspicious.
- Interrupt and resume: stop the worker while the run waits for approval; restart; approve → resumes once from the checkpoint.
- Budget: set a tiny cap → the run stops cleanly with a clear status, and the safety reserve still works for critical tasks.
- Rule of Two: write down which of the three properties this run has; there must be at most two without a human approval.
- Evals: run the eval suite; compare with the last accepted report; record cost per item.
- Records: every step left an activity record with the employee, profile version, task and correlation ID; the session state was not used as the business record.

### Automation

- Replay the trigger several times (and concurrently): one execution, one business effect (Scenario G pattern).
- Make the handler fail: retries follow the policy, then the dead-letter state and an incident; nothing is half-applied.
- Trigger it as an employee without the grant, and with a trigger kind not in `callable_from`: refused, recorded, no effect.
- Check that no model is called unless the specification names an AI step.

### Registry change (employee, automation, tool, advisor)

- `just registry` passes and the diff shows exactly the intended grants, policies and sections.
- An employee or automation becomes `ready` only with every section defined and its evaluation report linked; nobody's autonomy, tools or budget change without the owner's approval in the pull request.

### Model gateway or budget
- Fake primary provider failing → fallback used, both attempts logged.
- Reservation above cap → refused before any provider request.
- Usage rows match the provider's reported tokens for a live smoke call (owner-approved, tiny budget).
- Kill switch → no calls within one polling interval.

### Security-sensitive change (auth, sessions, permissions, secrets)
- Re-run the full two-tenant matrix and the permission matrix tests.
- Try privilege escalation: change role IDs, shop IDs and organization IDs in requests; replay old sessions; skip MFA steps.
- Check headers and cookies (`__Host-`, `Secure`, `HttpOnly`, `SameSite=Strict`) and CSRF rejection.
- Grep logs from the run for tokens, emails and addresses.
- Update `security.md` with the ASVS IDs verified.

### Deployment or infrastructure change
- `doctl apps spec validate` on the app spec; build the image; scan it.
- Deploy to the target environment (owner-started for production); check `/readyz`, logs and one key journey.
- Roll back once if the change is risky, and roll forward again.

## Severity of what you find

| Finding | Action |
|---|---|
| Data leak across tenants, secret exposure, unapproved external write, money error | Stop. Do not merge. Fix with a regression test. Tell the owner immediately. |
| Wrong behaviour in an acceptance criterion | Fix before merge. |
| Missing edge case with low impact | Add the test and fix, or record an issue linked in tests.md with the owner's agreement. |
| Cosmetic | Note it; fix in the same slice if trivial. |

## Evidence format (append to `docs/features/<feature>/tests.md`)

```markdown
## Verification — <slice ID> — <date>

| Acceptance criterion | Covered by tests | Verified for real | Result |
|---|---|---|---|
| AC-4.4.1 | test_ingest_receipt__same_event_twice__creates_one_order | Replayed webhook 10× on local stack; 1 row in app.orders | Pass |

**Adversarial checks run:** <list>
**Observed:** request_id <id> traced through logs, audit row <id>, job <id>; no PII in logs (grep run).
**Deployed check:** <environment, what was done, result> or "not applicable".
**Open issues:** <links> or "none".
```
