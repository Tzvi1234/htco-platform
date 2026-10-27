# Activity records — Security

ASVS 5.0: 16.2.x (an audit-grade trail), 8.4.1 (tenant isolation), 14.2.x (sensitive data in records).

| Threat | Control |
|---|---|
| Buyer data, prompts or model output in the trail | Summaries come only from code templates with identifier parameters (no spaces, markup, braces or newlines). A `Summary` renders its own text and checks every copy (`replace`, `copy.replace`). The row names its template. The actor, correlation and causation ids are identifiers too, in the writer and as checks. There is no free-text column (AC-7.7.4) |
| Prompt injection through stored text | Nothing from an email, page, review or model is ever stored, so nothing can be replayed into a model later |
| Rewriting history | A row trigger refuses update and delete for every role, including the table owner; no application role holds TRUNCATE. Members have no write grant; the worker has insert only (AC-7.7.2) |
| Cross-tenant or cross-shop reads | RLS on the parent and on every partition, which have no direct grants. Members see their organization's records: a shop's records only with access to that shop, and organization-wide records only as an Owner or Admin, as the source tables (AC-7.7.3) |
| Cross-tenant writes | The shop foreign key is bound to the organization. The worker's policy checks its job's organization |
| Unbounded growth | Monthly partitions; a retention policy is decided with 7.8 and Phase 10 |
