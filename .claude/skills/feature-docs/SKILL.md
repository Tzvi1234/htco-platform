---
name: feature-docs
description: How to document every feature and decision in htco-platform so the owner and future sessions can understand what exists, why, and how to operate it. Use when starting a new feature (create the folder), at the end of every slice (update the five files, contracts, data model, plan status), and when making a decision worth an ADR. Also defines what must never be committed as documentation.
---

# Feature documentation

Documentation is part of the change, written in the same pull request. It must stay true: if the code changes, the docs change with it.

## The feature folder

Each feature has `docs/features/<feature-slug>/` created from `docs/features/_template/` at the start of its first slice. Slugs are short and stable (`orders-ingest`, `listing-health`, `auth-bff`).

| File | Holds | Update when |
|---|---|---|
| `overview.md` | What and why, users, requirements (FR IDs), acceptance criteria (AC IDs), status per slice, known limits, external facts learned (for example Etsy behaviour) | Start of the feature; each slice; when a fact is learned |
| `design.md` | Components and layers touched, contracts (endpoints, events, tables), state machines, sequence diagrams, key decisions and alternatives | Whenever the design changes |
| `tests.md` | Test plan, mapping of AC IDs to tests, verification evidence from the `feature-verification` skill | Every slice |
| `security.md` | One-page threat model (data flow with trust boundaries, STRIDE table, LINDDUN for personal data, agentic risks and Rule of Two for agent features), ASVS IDs covered | Every slice that touches security, data or agents |
| `operations.md` | Configuration and flags, metrics and alerts, runbook entries (symptom → check → fix), rollback, drills performed | Every slice that runs in production |

## At the end of every slice

1. Update the five files above as needed.
2. Update shared references the slice changed:
   - `packages/contracts/openapi.yaml` and event schemas (generated or edited first, contract-first);
   - `docs/architecture/DATA_MODEL.md` (tables created, personal-data class, retention);
   - `docs/architecture/EVENT_CATALOG.md`;
   - `docs/architecture/PERMISSION_MATRIX.md`;
   - `docs/architecture/THREAT_MODEL.md` if a new element or threat appeared;
   - `docs/product/ASSUMPTIONS.md` for facts verified or new assumptions;
   - `docs/cost/COST_MODEL.md` if costs changed;
   - `CLAUDE.md` and `README.md` if commands, structure or workflow changed.
3. Tick the slice in `docs/plan/phases/phase-NN-*.md`; when the phase completes, update the status table in `docs/plan/PLAN.md`.
4. The pull request description summarizes the change in plain language and links the feature folder.

## ADRs

Write an ADR in `docs/adr/` (template in `docs/adr/README.md`) when you add or replace a technology or provider, change a boundary or the deployment shape, accept a trade-off on purpose, or deviate from the engineering rules or an earlier ADR. Add it to the ADR index. Accepted ADRs are not edited except for status; a new ADR supersedes an old one.

## Writing style

- Plain English, short sentences, active voice. Write for a reader who was not in the conversation.
- Concrete over abstract: name the table, the endpoint, the event, the test.
- Link instead of repeating. One fact lives in one place.
- Mermaid for diagrams (renders on GitHub).
- Dates as YYYY-MM-DD. Mark anything unverified as such.
- Code identifiers in backticks; requirement and criterion IDs as written (`FR-ORD-02`, `AC-5.4.1`).

## Never commit

- Session notes, scratch files, TODO dumps, chat transcripts or "summary of what I did" files.
- Duplicated documents or copies of external docs.
- Empty placeholder files "for later".
- Secrets, tokens, real buyer data, screenshots containing personal data.
- Generated reports (coverage HTML, eval dumps) — they belong in CI artifacts.

If something is worth keeping, it belongs in one of the documents above.
