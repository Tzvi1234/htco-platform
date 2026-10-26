# ADR-0024: A shared knowledge base on pgvector, hybrid retrieval after structured tools

- **Status:** Accepted (owner approved the decisions, 2026-10-26)
- **Date:** 2026-10-26
- **Deciders:** Owner, engineering
- **Related:** [ADR-0004](0004-model-gateway.md), [ADR-0011](0011-data-access-and-migrations.md), [ADR-0014](0014-tool-registry.md), [ADR-0018](0018-order-history-and-buyer-data.md), [ADR-0023](0023-routing-modes-and-difficulty-assessment.md), [Phase 9](../plan/phases/phase-09-router-advisor.md) slice 9.10, [Phase 14](../plan/phases/phase-14-email-support.md) (Dana), DATA_MODEL §5

## Context

Dana (Customer Service, Phase 14) answers buyers from what the shop knows: its shipping, return and privacy policies, the FAQ, its listings and products, and how similar questions were answered before. Later employees need the same kind of lookup: Noam for translations, Daniel for listing audits, Shon for research. Facts that HTCO holds in structured form already have tools and must come from them. Examples are an order's state, a tracking number, a price, or a listing's stock. The rest is unstructured text that a model cannot be trusted to remember. It has to be retrieved, under the same tenant isolation, buyer-data and prompt-injection rules as everything else.

The constraints:
- Supabase Postgres in Frankfurt is the system of record, with RLS for tenant isolation (ADR-0005, ADR-0011).
- Buyer contact data (DC1) and buyer content (DC2) have strict storage and retention rules (ADR-0018, DATA_MODEL §5).
- Every model call goes through the Model Gateway (ADR-0004), and no model ID is written in code.

## Decision drivers

- Tenant isolation by RLS, organization and shop, in the same database as the source data.
- No buyer PII in what is stored or embedded. Raw buyer messages are never embedded.
- Facts come from structured tools. Retrieval only adds unstructured knowledge.
- Embeddings are routed, budgeted, masked and recorded like any other model call.
- One capability for every employee, behind a port, so the store could change later.
- No new external service holding shop data.

## Options considered

1. **pgvector inside our Supabase Postgres** (chosen). Vectors live next to the rows they describe, under the same RLS, backups, region and migrations. The org and shop filter goes in the same SQL statement as the similarity search, and so does Postgres full-text search.
2. **An external vector service** (Cloudflare Vectorize, Pinecone and similar). This was rejected. It is a second store of shop data, outside RLS, with tenant isolation enforced only by our code. It would add another region and terms to review, and another key. Filters and full-text would be split across two systems.
3. **Full-text search only.** This was rejected as the only method. It misses paraphrases, and HTCO answers in Hebrew and English: Postgres has no Hebrew stemmer, while vector similarity is language-agnostic. It is kept as one half of the hybrid.

## Decision

### 1. Store: pgvector in Supabase Postgres

The `vector` extension is enabled in the `extensions` schema. There are two tables (DATA_MODEL, Phase 9):

**`knowledge_sources`:** one row per ingested source. It holds:
- the organization, and the shop (null for organization-wide documents);
- `kind`, `source_ref` (for example the listing ID or the policy key);
- `content_hash`, `version`, `status` (`active`, `quarantined` or `deleted`);
- `derived_from_conversation` (for approved replies), so an erasure request can find them;
- `retain_until`.

**`knowledge_chunks`:** the redacted chunk text, its language and a `tsvector`, plus:
- `embedding vector(n)` and the embedding alias that made it;
- the chunk's position, organization, shop and source.

The organization and the shop are bound by composite foreign keys, as on the other shop tables. Indexes:
- HNSW on `embedding` (cosine);
- GIN on the `tsvector`;
- B-tree on `(organization_id, shop_id)`.

RLS is on for both tables:
- Members read only their organizations' rows of the shops they may access (`user_org_ids()` and `user_shop_ids()`). Organization-wide rows, with a null `shop_id`, are visible to every member of that organization, including shop-scoped roles, and to no other organization.
- The worker, which runs ingestion and the agents' searches, sees only its job's organization (`worker_organization_id()`). That RLS is organization-wide, so **shop isolation for an employee's search rests on the scope the search applies**, and that scope comes only from the task's session (§5).
- No client role writes. Writes happen only through the ingestion use case.

**Classification (ER-DATA-07).** `shop_policy`, `faq`, `listing` and `product` chunks are shop or organization content. `approved_reply` chunks and `knowledge_sources.derived_from_conversation` are **derived from DC2**. They are redacted, but they keep DC2's 365-day retention and buyer-erasure rules (§7).

### 2. What may be embedded

There is an allowlist of source kinds. Nothing else is ingested:

| Kind | Content | Notes |
|---|---|---|
| `shop_policy` | The shop's shipping, returns and privacy policies; HTCO policy documents for the shop | Organization or shop |
| `faq` | Questions and answers the owner wrote | Owner-edited |
| `listing` | A listing's public text: title, description, attributes, materials, sizes | Not price, stock or state: those come from tools |
| `product` | Internal product information useful for answers: specifications, care, compatibility | Not supplier, cost or margin fields |
| `approved_reply` | A reply that a person **approved** (and that was sent), after PII redaction | Never the buyer's message it answered |

**Never embedded:** raw buyer messages or emails (DC2), and buyer names, contacts or addresses (DC1). Order and tracking data are left out too, because tools answer them.

Approved replies pass a **deterministic redaction** step before chunking. It removes or replaces names (including the buyer's name from the linked order record), email addresses, phone numbers, postal addresses, tracking numbers and order numbers (replaced with placeholders such as `[order]`). The gateway's PII masking then applies again at embedding time, as a second layer.

**Buyer text quoted inside a reply** is not an identifier, so redaction alone cannot remove it, and no matcher can find every echo of it. So it is never given the chance to appear:
- **An approved reply is embedded only from HTCO's own clean copy.** That is the reply text as composed and approved in HTCO, stored when it is approved, before any quoted history or signature is added for sending.
- **Text that may contain quoted buyer words is never embedded.** That covers the sent email, a mailbox copy, and an imported or legacy reply without a clean copy.
- **The drafting rule** for Dana's replies is not to quote the buyer. The clean copy still passes the deterministic redaction and the residual check below.

There is no overlap matcher. The fixtures show that the sent version of a reply, which carries quoted history and inline echoes, is never what gets embedded, and that a reply with no clean copy is refused.

A chunk in which the residual check still finds PII, or quoted buyer text, is **quarantined**: it is not embedded, and it is reported.

### 3. Retrieval order: tools, then hybrid search, then the model

1. **Structured tools first.** Order state, listing state, tracking, prices and stock come from their read tools, never from the knowledge base. None of that is indexed.
2. **Hybrid search through `knowledge.search`.** This is **one SQL statement** whose `WHERE` clause applies the organization, the shop and the allowed kinds before any ranking. It combines Postgres full-text search with vector similarity (the `simple` configuration for Hebrew, `english` for English) and fuses the two ranked lists by reciprocal rank fusion. The `tsquery` is built with `websearch_to_tsquery` or `plainto_tsquery`, never `to_tsquery` on raw text, and the query is capped in length before it is parsed or embedded. The result is bounded: `k` is at most 10, and there is a minimum score. Each hit carries its kind, source, version, chunk ID and score.
3. **The model last.** The employee's model composes the answer from the tool results and the retrieved chunks, and cites the chunk IDs it used.

### 4. Embeddings through the Model Gateway

The catalog gains the `embedding` capability and an alias such as `default-embedding`. That alias names the embedding model, never a model ID in code. Every embedding call goes through the gateway: indexing a chunk, and embedding a query at search time. That means routing (the alias, ADR-0023's pinned semantics apply), budget reservation and settlement, PII masking, and one `routing_decisions` row and one `model_usage` row. Ingestion is billed to the organization and shop, under a **system routing policy, `knowledge-ingest`**. It is pinned to the embedding alias with `max_data_class = internal`, so the gateway's data-class check has a policy to apply even though no employee is calling. Query embeddings are billed to the searching employee and the task, under that employee's policy. Every query is **PII-masked by the gateway before it is embedded**, because Dana builds her queries from buyer messages.

Changing the embedding model is a catalog change to the alias. It also triggers a full **re-embed** job for the affected rows, because vectors from different models are not comparable. Each chunk records the alias and model that produced it, and a search only compares vectors from the same model.

### 5. A shared capability behind a port

`KnowledgeBase` is the application port: `ingest(source)`, `search(query, scope, policy)` and `delete(source)`. **`scope` is the organization and shop from the task's session (ER-AI-12, the Phase 8 tool bridge), never from tool arguments.** The tool's input schema has no organization or shop field and rejects one that the model supplies. The pgvector adapter lives in persistence, and its embeddings go through the gateway port. An in-memory adapter for tests passes the same contract suite. A future external vector store would need its own ADR, and the tenant-isolation and residency questions above, before it could sit behind the port.

Employees use the capability through the registry tool **`knowledge.search`**: `side_effect = "none"`, `data_scope = "internal"`, audited. Each employee's sheet sets a **knowledge policy**: which kinds it may search, its maximum `k`, and its languages. The tool bridge and the policy engine enforce it. Dana is the first consumer (policy, FAQ, listing, product, approved replies). An employee without the grant is denied. The search is recorded as an activity: the employee, the task, a **keyed hash** of the query (an HMAC with a server-side key, never its text, which may contain a buyer's words; a plain hash of a short query such as a name could be reversed by guessing), the result IDs and the latency.

### 6. Ingestion and refresh

Ingestion runs as idempotent `knowledge.ingest` jobs on the queue, keyed by the source and its content hash. It is triggered by:
- a listing change (the listing sync and Etsy's listing webhooks);
- a shop-policy change (the policy reconciliation);
- an FAQ edit (the owner's screen);
- an approved and sent reply (Phase 14's send event).

An unchanged source (same hash) makes no embedding call. A changed source replaces its chunks in one transaction. A nightly reconciliation compares sources with the stores they come from, and fixes drift. Failures go to the dead letters, as with every job.

### 7. Deletion and retention

- A source that is deleted or deactivated (a listing removed, a policy replaced, a reply retracted) loses its chunks **in the same transaction**, as a hard delete. No chunk may outlive its source.
- **Shop disconnect:** the shop's knowledge is purged with the rest of its data, under ADR-0018's 30-day rule.
- **Approved replies** are derived from buyer conversations. They follow DC2 retention (365 days from the reply), even after redaction. A buyer's erasure request deletes the approved-reply chunks linked to that buyer's conversations.
- Embeddings and chunks are never exported to the eval sets.

### 8. Retrieval quality

There is a versioned eval set of at least 200 labelled queries, in Hebrew and English, across the source kinds, each mapped to its expected chunk or chunks. It is synthetic plus owner-reviewed, with no buyer PII. It runs in the Phase 8 harness. For hybrid, full-text only and vector only, it reports recall@5, MRR and p95 latency. **Cross-tenant and cross-shop leakage must be zero**: this is a hard gate, checked by seeding two organizations and two shops. The thresholds are agreed at kickoff. Dana's activation (14.7) requires a passing run on her shop's knowledge.

### 9. Prompt injection: retrieved text is data

Retrieved chunks reach the model only inside a delimited, quoted reference block with their provenance (kind, source, version). They never appear as system or developer instructions. Nothing in retrieved text can:
- change an employee's tools, grants, routing or policies;
- call a tool by itself;
- promise what the reply policy forbids.

The ingestion allowlist keeps the obvious attack surface small, because buyer-written text never enters. Instruction-like content in an allowed source (for example a listing description) stays data: it is flagged in the source's record, not executed. An **injection corpus** is ingested into a test shop. The evaluation shows that it causes no tool call, no grant or policy change, and no unapproved promise.

## Consequences

- **Positive.**
  - Answers are grounded in the shop's own knowledge, under the same RLS, backups and region as the rest of its data.
  - There is one capability for every employee, and the store can be replaced behind the port.
  - There is no new vendor.
- **Negative.**
  - Vectors add storage and index maintenance to Postgres. The HNSW index's memory has to fit the Supabase plan, so it is sized at kickoff from the corpus.
  - Changing the embedding model means a re-embed.
  - Redaction must be kept up to date with new PII shapes.
- **Follow-ups.**
  - Slice 9.10 builds it.
  - Phase 14's Dana consumes it (14.4 drafts, 14.7 activation).
  - Later employees get their knowledge policies when they are activated.

## Validation

The acceptance criteria are AC-9.10.1 to AC-9.10.12 in Phase 9, with pgTAP for RLS, the redaction corpus, the injection corpus and the retrieval eval set.

## Validation log

- 2026-10-26: Decisions approved by the owner (pgvector in Supabase; tools, then hybrid search, then the model; embeddings through the gateway; a shared capability through `knowledge.search`; a new slice 9.10 before Dana).
- 2026-10-26: The security review of the ADR (PR #130, before any 9.10 code) added eight hardening points, folded into §1, §2, §3, §4 and §5 and into AC-9.10.1, 3, 4, 5 and 9. The decisions above are unchanged. The points: organization-wide rows visible to the whole organization; shop isolation for a worker search resting on the session's scope; approved-reply chunks classified as derived from DC2; quoted buyer text kept out (made stricter the same day after two local Codex reviews of #136: only HTCO's clean copy of an approved reply, from before any quoting is added, is ever embedded, and there is no matcher); `websearch_to_tsquery` or `plainto_tsquery` only, with a length cap; the `knowledge-ingest` system policy; queries masked before embedding; a keyed query hash (HMAC).
