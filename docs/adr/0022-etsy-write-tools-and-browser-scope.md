# ADR-0022: Every Etsy API endpoint as a tool, writes included, under approvals; browser actions only inside the recorded Etsy scope

- **Status:** Accepted for the API part (owner's brief, 2026-10-23). Browser part accepted with every action **off** until its scope is recorded ([etsy-automation-approval.md](../architecture/etsy-automation-approval.md))
- **Date:** 2026-10-23
- **Deciders:** Owner, engineering
- **Amends:** [ADR-0008](0008-etsy-integration.md) ("read-first": writes become tools, behind the controls below)
- **Related:** [ADR-0014](0014-tool-registry.md), [ADR-0019](0019-unified-execution-engine-and-action-gateway.md), [ADR-0020](0020-per-shop-browser-environment.md), [Etsy API capabilities](../architecture/etsy-api-capabilities.md), [multi-shop policy](../architecture/etsy-multi-shop-policy.md)

## Context

The owner wants everything Etsy's Open API v3 allows, as dashboard features and as tools for agents and automations. That includes writes:

- listings: create, update, activate and delete;
- media: images, video and files;
- inventory, price and stock;
- shipping profiles, return and processing policies, and sections;
- receipt updates and `createReceiptShipment`;
- `updateShop` and vacation mode.

He will try every write first himself, on purpose, and accepts that a write may fail the first time.

He also reports that Etsy approved the relevant **browser automations**. The approval was verbal, in a conversation with people he knows at Etsy. He has no document yet and can get a signed one (owner, 2026-10-23). Until now the plan said "no scraping" and "nothing signs in to Etsy from our servers" (Phase 14 delivery B).

## Decision drivers

- Use the whole API, because it is the sanctioned channel.
- Never surprise the live shop. LeatherPY is live, and no test or agent writes to it without the owner's explicit trigger.
- Approvals, audit and verification on every write, through one path (the Action Gateway).
- Browser automation on Etsy only inside a scope that is written down, can be checked, and can be switched off per action.

## Decision

### API

1. **An inventory of every endpoint.** All 104 operations of the live OpenAPI spec are mapped to 38 draft tools in [`registry/tools/etsy_open_api.toml`](../../registry/tools/etsy_open_api.toml): 20 read tools and 18 write tools. The table in [etsy-api-capabilities.md](../architecture/etsy-api-capabilities.md#full-endpoint-inventory) gives each operation's scope, feature, tool, draft grants, phase and approval policy.
2. **Approval policies.**
   - **R0:** read, no buyer data.
   - **R1:** read with buyer or account data. Masked by role and audited where personal.
   - **W1:** a write. Needs an approval decided by the Owner or an Admin, goes through the outbox with an idempotency key, and the result is re-read and verified. Audited.
   - **W2:** W1 plus step-up re-authentication and a per-action feature flag. Used for changes every buyer sees, price and stock, policies, and receipts or shipments.
   - **W3:** W2, decided by the Owner only. Used for deletion and for state changes that charge Etsy's listing fee (`financial` side effect).
3. **The owner tries each write first.**
   - Every write tool starts **owner-trigger only** on every shop: only the Owner can run it, by hand, from the dashboard.
   - After the first successful, verified run on a shop, the Owner may open it to automations, and then to employees (always as approvals) for that shop.
   - The first run's result, error or success is recorded. A failure is shown to him as it is, with Etsy's error.
4. **Write scopes need re-consent.** `shops_w`, `listings_w`, `listings_d` and `transactions_w` are requested only when the Owner switches on the first write. Etsy's consent screen runs again, and the new scopes are recorded and probed (slice 4.5). A tool whose scope is missing is refused before any call.
5. **No write without the owner's trigger.** CI and tests run against fakes and recorded fixtures only. The adapter refuses a write whose execution has no owner-trigger record or decided approval. The live shop LeatherPY is never the target of a test.
6. **Employees never write from a session.** An employee's call to a write tool becomes an approval through the Action Gateway at every autonomy level, until the Owner changes that tool's policy. The write executes under the approval's execution identity after the decision (this amends AC-11.5.3: the path stays unreachable from inside a session).

### Browser

7. **A recorded scope is the only source of permission.**
   - [etsy-automation-approval.md](../architecture/etsy-automation-approval.md) lists every candidate browser action on etsy.com, and the owner fills in the scope for each one.
   - The approval is recorded as **"verbal, per the owner, 2026-10-23, written confirmation pending"**.
   - Each action has its own feature flag (`browser.etsy.<action>`). It is **off** until the owner records that action's scope, and it can be switched off at any time.
   - The engine checks the flag and the recorded scope version at execution time, not only at deploy.
8. **Inside the scope, the honest environment only** ([ADR-0020](0020-per-shop-browser-environment.md)): human pace, challenges to a person, no evasion.
9. **Risk recorded.** A verbal approval from Etsy employees may not bind Etsy's Trust & Safety team or override the Terms of Use. If Etsy enforces against a browser action, the approval may not protect the shop. Therefore:
   - the written confirmation is requested before any browser **write** action is switched on (recommended);
   - the first browser actions are read-only exports (reports the API lacks);
   - any warning, challenge spike or restriction switches every Etsy browser flag off, and the action falls back to a person.

## Consequences

- Positive:
  - the full API is usable, with one review point per write;
  - the owner sees real write behaviour on his own shop before anything is automated;
  - browser automation is narrow, flagged and evidenced.
- Negative:
  - more tools to build and test (38 definitions, built phase by phase);
  - the browser scope depends on a verbal approval until it is written;
  - re-consent for write scopes needs the owner at the keyboard.
- Follow-ups:
  - the write-tool slices in Phases 6, 7, 11, 12 and 13;
  - the browser action catalog in Phase 8A;
  - written confirmation from Etsy ([ASSUMPTIONS D-16](../product/ASSUMPTIONS.md#1-owner-decisions-needed)).

## Validation

- A test walks every write tool:
  - it is refused without an owner-trigger record or a decided approval;
  - it is refused without its scope;
  - it is never reachable from an employee session.
- A browser action whose flag is off, or whose recorded scope is missing or older than the flag, is refused before a browser starts.

## Validation log

- 2026-10-23 — The owner reported Etsy's approval as verbal. The scope table was created empty, and every Etsy browser flag defaults to off.
