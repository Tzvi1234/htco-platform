# Activity records — Security

ASVS 5.0: 16.2.x (an audit-grade trail), 8.4.1 (tenant isolation), 14.2.x (sensitive data in records).

| Threat | Control |
|---|---|
| Buyer data, prompts or model output in the trail | Summaries come only from code templates with identifier parameters (no spaces, markup or braces). The row names its template. There is no free-text column (AC-7.7.4) |
| Prompt injection through stored text | Nothing from an email, page, review or model is ever stored, so nothing can be replayed into a model later |
| Rewriting history | Append-only by trigger for every role, including the table owner. Members have no write grant; the worker has insert only (AC-7.7.2) |
| Cross-tenant or cross-shop reads | RLS on the parent and on every partition, which have no direct grants. Members see their organization's records, and a shop's records only with access to that shop (AC-7.7.3) |
| Cross-tenant writes | The shop foreign key is bound to the organization. The worker's policy checks its job's organization |
| Unbounded growth | Monthly partitions; a retention policy is decided with 7.8 and Phase 10 |
