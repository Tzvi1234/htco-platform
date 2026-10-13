# HTCO OS — System Threat Model

| | |
|---|---|
| **Status** | Baseline 1.0; reviewed at the end of every phase |
| **Last updated** | 2026-10-14 |
| **Method** | The four questions of the Threat Modeling Manifesto; STRIDE per element; LINDDUN GO for personal data; OWASP Top 10 for Agentic Applications (2026) for the agent layer |
| **Standards** | OWASP ASVS 5.0 Level 2 (plus pulled-forward items, see ER-SEC-11), OWASP AISVS 1.0 for AI components, OWASP Multi-Tenant Security Cheat Sheet |

Each feature adds its own one-page threat model in `docs/features/<feature>/security.md`. This document is the system baseline those pages build on.

## 1. What are we working on?

### Assets

| Asset | Why it matters |
|---|---|
| Etsy OAuth tokens and API keys | Full control of a shop's listings and orders |
| Buyer personal data (names, addresses, messages) | Privacy law, Etsy terms, trust |
| Money figures and supplier mappings | Wrong numbers lead to losses; a swapped supplier mapping redirects purchases |
| Approval decisions and audit log | The basis of accountability for autonomous work |
| Model provider keys and budgets | Direct financial exposure |
| Agent policies, prompts and memory | Control over what agents do |
| Owner account and MFA factors | Root of trust for everything above |

### Trust boundaries

```mermaid
flowchart LR
  subgraph Internet[Untrusted]
    browser([Browsers])
    native([Native apps])
    etsyhook[Etsy webhooks]
    mailin[Inbound email]
    web[Supplier and competitor pages]
    llmout[Model outputs]
  end
  subgraph Edge[Boundary 1 · edge]
    ingress[DigitalOcean ingress · TLS]
  end
  subgraph App[Boundary 2 · application]
    api[API]
    worker[Workers]
    agents[Agent runtime]
  end
  subgraph Data[Boundary 3 · data]
    db[(Postgres with RLS)]
    secrets[(Encrypted tokens)]
  end
  browser --> ingress --> api
  native --> ingress
  etsyhook --> ingress
  mailin --> api
  api --> db
  worker --> db
  agents -->|tools via policy engine| worker
  agents -.reads untrusted content.-> web
  llmout -.returned to.-> agents
  worker --> secrets
```

## 2. What can go wrong? (STRIDE)

| # | Element | Threat (STRIDE) | Control | Verification |
|---|---|---|---|---|
| T1 | API, data | **Information disclosure**: one tenant reads another tenant's data (IDOR, missing filter) | Server-side authorization from the verified identity; RLS with `set local` claims; tenant IDs from clients are only selectors | Two-tenant test matrix over every route; pgTAP RLS tests on every table; `rls_enabled` inventory test |
| T2 | Webhook endpoint | **Spoofing / tampering**: forged or replayed Etsy or email webhooks | Standard Webhooks signature on the raw body; 5-minute timestamp tolerance; inbox unique on event ID | Tests with invalid, expired, replayed and modified payloads |
| T3 | Order ingest | **Tampering / repudiation**: duplicate events inflate sales or duplicate work | Idempotent upserts keyed by external IDs; `updated_timestamp` comparison; audit | Replay 10× test; out-of-order test |
| T4 | Agent runtime | **Elevation of privilege**: a model requests a tool it is not allowed to use | Deny-by-default policy engine per call; capabilities per agent profile; tenant and actor from the run | Unauthorized tool call denied and audited, even when the model insists |
| T5 | Agent inputs | **Tampering** (prompt injection) through product pages, emails or messages | Untrusted content kept in data fields and marked; Rule of Two; no tools in quarantined extractors; schema-validated outputs; approvals for external effects | Injection fixture corpus in CI asserts no side effect and no permission change |
| T6 | Tokens and keys | **Information disclosure**: OAuth or API secrets leak through logs, repos, clients or prompts | Application-level encryption of tokens; secrets only in environment stores; log redaction; secret scanning | Log-scan test for token patterns; gitleaks on every push; tokens never present in tool results |
| T7 | Outbox, Etsy writes | **Tampering / repudiation**: duplicate external writes (two shipment emails to a buyer) | Outbox with unique idempotency keys; re-read before non-idempotent writes; human confirmation for shipment writes | Crash-between-steps tests; replay of outbox delivery |
| T8 | Debugger agent | **Elevation of privilege**: an automatic fix changes money, permissions or production code | Allowlisted, reversible remediations only; everything else needs approval or a developer | Attempted non-allowlisted remediation is blocked |
| T9 | Workers and queue | **Denial of service**: poison messages, retry storms, backlog | Bounded retries with backoff and jitter; dead-letter state; per-shop locks; alerts on queue age | Poison message lands in dead-letter; backlog alert test |
| T10 | Model gateway | **Denial of service (financial)**: runaway agent loops or prompt amplification | Budget reservation before calls; caps per task, agent, shop and day; recursion limits; kill switch | Cap reached → call refused; loop test hits recursion limit |
| T11 | Web client | **Spoofing / information disclosure**: XSS steals sessions, clickjacking | BFF with `__Host-` HttpOnly cookies; CSP; `frame-ancestors 'none'`; output encoding; CSRF header | Cookie attribute tests; CSP header test; CSRF rejection tests |
| T12 | Native clients | **Information disclosure**: stolen device, deep-link hijack, clipboard and screenshot leaks | Keystore or Credential Locker; verified App Links; re-authentication for high-risk approvals; masked PII | Platform security test checklist per release |
| T13 | Supply chain | **Tampering**: a compromised dependency, action or image | Pinned versions with minimum release age; SHA-pinned actions; scanners; SBOM | Renovate policy; zizmor and actionlint in CI |
| T14 | Owner account | **Spoofing**: account takeover | Mandatory TOTP; leaked-password protection; session limits; new-device sign-in recorded (Phase 3) and emailed (Phase 6) | MFA-required tests on owner-only routes; new-device notification test |
| T15 | Audit log | **Repudiation**: actions without a trace or edited history | Append-only schema with no update or delete grants for application roles | Grant test; audit written for every money, permission, publishing and approval action |
| T16 | Backups and restore | **Denial of service / information disclosure**: data loss, or restored data crossing tenants | Daily backups, media backup job, restore drills, tenant checks after restore | Restore drill record before production data is relied on |
| T17 | Browser worker (later) | **Elevation of privilege**: automated browser used to reach internal services or act beyond scope | Separate container, outbound allowlist, no internal network, URL and action allowlists, stops at CAPTCHA and MFA | Isolation test before enabling (ADR required) |

## 3. Agent-specific risks (OWASP Top 10 for Agentic Applications, 2026)

| ID | Risk | HTCO control |
|---|---|---|
| ASI01 | Agent goal hijack | Untrusted content in data fields with spotlighting; goals come only from tasks created by authorized actors |
| ASI02 | Tool misuse and exploitation | Narrow typed tools only (no generic HTTP, SQL or shell); strict schemas; policy check on every call |
| ASI03 | Identity and privilege abuse | Separate service identity per agent profile; tenant and actor taken from the run; least-privilege database roles |
| ASI04 | Agentic supply chain | Pinned agent framework and SDK versions; no third-party tool servers in production without review; prompts and policies versioned in the repository |
| ASI05 | Unexpected code execution | No code-execution tools; no `eval`; browser worker isolated |
| ASI06 | Memory and context poisoning | Memory writes only through validated code paths with source, shop, confidence and expiry; memory scoped per tenant |
| ASI07 | Insecure inter-agent communication | Handoffs through database events and resource references, authorized like any other write |
| ASI08 | Cascading failures | Circuit breakers per provider; bounded retries; dead-letter; budgets |
| ASI09 | Human-agent trust exploitation | Approvals show the raw action, parameters, evidence and diff, not just an agent's summary; what was shown is logged with the decision |
| ASI10 | Rogue agents | Global and per-agent kill switches (tested); behaviour baselines from evals; each profile has an owner and a review date |

## 4. Privacy (LINDDUN GO summary)

| Threat type | Where it applies | Control |
|---|---|---|
| Linking and identifying | Buyer data across shops and orders | Data minimization; buyer data stays shop-scoped; no cross-shop buyer profiles |
| Data disclosure | Logs, exports, model prompts, support views | Masking by default; export permission plus audit; PII masked before model calls unless required |
| Unawareness | Buyers do not know HTCO processes their data | Processing limited to fulfilling and servicing their orders, as Etsy's terms allow; privacy notice for the owner's business |
| Non-compliance | Retention beyond need; marketing use of buyer data | Retention policy per data class; consent ledger; no marketing lists from Etsy buyer data |

## 5. What are we going to do about it?

Controls are built into the phases where the risk first appears:

| Phase | Controls delivered |
|---|---|
| 1 | Secret scanning (gitleaks), dependency and static-analysis scans, SHA-pinned actions, error envelope without internals, structured logs without PII, error monitoring with PII scrubbing |
| 2 | RLS and authorization model, MFA enforcement, audit schema, two-tenant test matrix |
| 3 | BFF sessions, CSRF, CSP and security headers, step-up re-authentication, password reset and MFA recovery, new-device sign-in records |
| 4 | Token encryption, OAuth state and PKCE, capability probes |
| 5 | Webhook verification and rate limits, inbox and outbox, idempotency keys, dead-letter, queue alerts, restore and kill-worker drills |
| 6 | Security notifications by email (new device, MFA change, shop connected or disconnected) |
| 7 | Approvals with server-side re-checks, policy engine, self-approval prevention |
| 8 | Agent controls ASI01–ASI10, budgets, injection corpus, kill switches, validated memory writes, PII masking before model calls |
| 10 | Media backup job and restore test |
| 12 | Inbound email quarantine and attachment policy |
| 16 | External security review against ASVS L2, restore drill, incident response runbook |

## 6. Did we do a good enough job?

At the end of every phase: re-read this page, add new elements and threats, confirm each control in the phase has a passing test, and record residual risks in [ASSUMPTIONS.md](../product/ASSUMPTIONS.md) with an owner.
