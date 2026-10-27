# Web UI coverage: every feature and its web step

| | |
|---|---|
| **Status** | 1.0 (2026-10-27): the owner's rule, "every feature we build must have a step in the plan that adds it to the dashboard" |
| **Rule** | [ER-UX-07](../engineering/ENGINEERING_RULES.md#13-user-experience-accessibility-and-language-er-ux): a slice that produces something a user should see or operate (data, a status, a setting or an action) names its web step, a UI slice or a UI acceptance criterion in the same phase or a later named one. A slice with nothing to show says "No UI: <reason>". The web comes first ([ADR-0016](../adr/0016-web-first.md)): Android (16) and Windows (17) follow it |
| **Maintained** | With the plan: a pull request that adds or changes a user-facing slice updates its row here, and plan review checks it ([PLAN.md §5](PLAN.md#5-gates-that-apply-to-every-phase), gate 9) |

"New" marks a web step added on 2026-10-27 by the audit that started this page. Slices with nothing to show, such as tooling, CI, schemas, ports, guards, drills and acceptance scenarios, are left out. They pass as "No UI".

## Phases 2 to 5

| Feature (backend slice) | What the user sees or does | Web step | Phase |
|---|---|---|---|
| 2.4 Policy engine v0, `/v1/me`, `/v1/shops` | User, role, granted shops | 3.7, AC-3.7.1 and 3.7.2; the shop switcher (3.3) | 3 |
| 2.5 Audit writer | The organization's audit records | **AC-3.9.3 (new):** the audit view | 3 |
| 2.6 User and shop-access management | Invite, roles, grants, remove, the one-time link | 3.9, AC-3.9.1 and 3.9.2; **AC-3.9.4, 3.9.5 (new)** | 3 |
| 3.5 BFF authentication | Sign-in, enrolment, reset, sessions, step-up, a second app, the owner-assisted reset | AC-3.5.1, 3.5.3, 3.5.6, 3.5.9; **AC-3.5.10, 3.5.11, 3.9.6 (new)** | 3 |
| 4.1 Etsy adapter | The remaining daily quota | AC-4.1.2 (the shop card) | 4 |
| 4.2 Token vault | Token health and key metadata | 4.6; **AC-4.6.3 (new)** | 4 |
| 4.3–4.5 Connect, refresh, probes | Connect and reconnect, `needs_reauth`, capabilities | 4.6, AC-4.4.2, 4.5.1, 4.6.1, 4.6.2 | 4 |
| 4.7 Disconnect | Disconnect, purge date | 4.6; **AC-4.7.3 (new)** | 4 |
| 5.1 Job queue and worker | Dead letters, worker state | 5.9, AC-5.9.2 | 5 |
| 5.3 Webhook ingress | Whether webhooks arrive | **AC-5.9.5 (new):** webhook intake on Admin › Health | 5 |
| 5.4 Receipt ingest, Product Sales Counters | Orders, timeline, units sold | 5.7; **AC-5.11.8 (new):** units and orders on Products | 5 |
| 5.5 Reconciliation and backfill | Freshness, manual sync, drifts | AC-5.7.1, 5.9.3; drifts as incidents in 6.5 | 5, 6 |
| 5.10, 5.11 Automation engine and order automations | Executions, retries, readiness | 5.9, AC-5.9.2, 5.11.6; Admin › Automations; 8.9; 15.5 | 5, 8, 15 |
| 5.15 Breaker, rate and concurrency | Why a sync is deferred | **AC-5.9.6 (new):** circuit breakers on Admin › Health | 5 |
| 5.16 Shop Environment Registry | Environment status | 8A.9, AC-8A.9.3 | 8A |
| 5.18 Status management, the chain view | An execution's chain | Admin › Health (htco-clients #60), 15.5, AC-7.8.3, 10.7.1 | 5, 7, 10, 15 |

## Phases 6 to 8B

| Feature (backend slice) | What the user sees or does | Web step | Phase |
|---|---|---|---|
| 6.1 Tracking detection | The order's tracking, its source, first sighting | **AC-6.5.3 (new):** tracking on the order timeline | 6 |
| 6.2 SLA policies | The windows and the business-day calendar | **AC-6.5.4 (new):** SLA policies and the calendar | 6 |
| 6.3 Incidents | Incidents, and moving one along | 6.5, AC-6.5.1; **AC-6.5.5 (new)** | 6 |
| 6.4 Notifications and email | The bell, emails, delivery failures | 6.5, AC-6.5.2; **AC-6.5.6 (new):** email failures | 6 |
| 6.6 Guarded shipment write | Confirm a tracking number | 6.6, AC-6.6.1 | 6 |
| 6.8 Owner-triggered writes | Each tool's state per shop, missing scopes | AC-6.8.2; **AC-6.8.4 (new)** | 6 |
| 6.9 Ship-by, holidays, low reviews | Alerts, the calendar, ship time per country | 6.5, AC-6.9.2, 6.5.4; **AC-6.9.3 (new):** ship time per country | 6 |
| 6.10 AutoVA tracking relay | Relay items, paste, copy | AC-6.10.1; **AC-6.10.5 (new):** relay items listed | 6 |
| 7.1, 7.11 Tasks and the Human channel | Working a task | 7.5; **AC-7.5.4 (new)** | 7 |
| 7.3 Approvals | Approvals to decide | 7.5, AC-7.5.1 to 7.5.3 | 7 |
| 7.6 Notification preferences | Each user's settings | **AC-7.6.2 (new)** | 7 |
| 7.7, 7.8 Activity | The activity record | 7.8, AC-7.8.4 | 7 |
| 7.9 Scheduled reporting | Daily and weekly reports | **AC-7.9.4 (new):** reports on the web | 7 |
| 7.12 The owner's first Etsy writes | Announcement, sale message, vacation, sections, holidays | AC-7.12.1, 7.12.2; **AC-7.12.3 (new)** | 7 |
| 7.13, 7.17 Suppliers and quotes | Request states, mappings | 7.16, AC-7.16.2; **AC-7.16.3 (new)** | 7 |
| 7.14 Quote mailbox | The mailbox's state, Gmail consent (b) | **AC-7.14.4 (new)** | 7 |
| 7.15 Quote parser | "Check this quote" | **AC-7.15.4 (new)** | 7 |
| 8.2 Registry API | Version history | 8.9; **AC-8.9.3 (new)** | 8 |
| 8.5 Messages and handoffs | Messages and handoffs | AC-8.5.3 (7.8); 15.8 | 7, 15 |
| 8.6 Kill switches | Every switch and its state | 8.9, AC-8.9.2; **AC-8.9.4 (new)** | 8 |
| 8.7 Evaluation harness | Evaluation results | 15.4, 15.6 | 15 |
| 8A.2a, 8A.2b Provider and configuration profiles | Provider, configuration, versions | 8A.9, AC-8A.9.3 | 8A |
| 8A.2c Browser dispatch | Worker startup checks | **AC-8A.8.3 (new):** browser workers on Admin › Health | 8A |
| 8A.3 Preflight and health | Failed checks, diagnostics | 8A.9; **AC-8A.9.5 (new):** health in words | 8A |
| 8A.4 Challenge handler | The Human task, the screenshot | **AC-8A.4.4 (new)** | 8A |
| 8A.5 Recipes | Exported stats and Ads rows; action flags | **AC-8A.5.4, 8A.9.6 (new)** | 8A |
| 8A.6 Browser Use for employees | Caps, spend, switches | 8.9, 9.8; **AC-9.8.10 (new)** | 9 |
| 8A.8 Monitoring and DLQ | Figures, the browser DLQ, alerts | **AC-8A.8.4 (new):** the same figures in the web app | 8A |
| 8A.10 AutoVA recipes | AutoVA orders, Unpaid alerts, the AutoVA sign-in | **AC-8A.10.6, 8A.9.7 (new)** | 8A |
| 8A.11 Live View and Takeover | The live session, control | 8A.11's screen, AC-8A.11.1, 8A.11.3 | 8A |
| 8B.2–8B.4 Authenticator | Enrol, rotate, remove, code view, grants | AC-8B.4.1 to 8B.4.3; **AC-8B.2.4, 8B.3.3, 8B.4.4 (new)** | 8B |

## Phases 9 to 21

| Feature (backend slice) | What the user sees or does | Web step | Phase |
|---|---|---|---|
| 9.1 Model Gateway | Provider switches, circuit state | 9.8; **AC-9.8.6 (new)** | 9 |
| 9.2 Model catalog | What is behind each alias | **AC-9.8.7 (new)**, read-only | 9 |
| 9.3 Router | Mode, policy; decision history | 9.8, AC-9.8.3 to 9.8.5; 15.6 | 9, 15 |
| 9.4 Budget ledger | Budgets, spend, caps reached | 9.8, AC-9.8.1, 9.8.2; **AC-9.8.8 (new)** | 9 |
| 9.5, 9.6 Decision Advisor and benchmark | Advisor status per family, the report | 9.8; **AC-9.8.9 (new)** | 9 |
| 9.7 Eitan | Incidents, tasks, suggested refund reasons | 6.5, 7.5, 7.8, 8.9, AC-9.7.6 | 9 |
| 9.9 Browser-agent budgets | Browser Use spend and stopped sessions | **AC-9.8.10 (new)** | 9 |
| 9.10 Knowledge base | Sources, the FAQ, the eval report | **AC-9.10.13 (new)** | 9 |
| 10.1–10.6 Monitors, findings, reconciliation, Noa | Monitor status, findings and acting on them, reports | 10.7, AC-10.7.1, 10.7.2; **AC-10.7.3 (new)** | 10 |
| Refund-reason analysis, repeat customers (FEATURE_BACKLOG) | Recurring refund causes, returning buyers | **The refund-pattern monitor, AC-10.7.4, 10.7.5 (new)** | 10 |
| 11.2–11.7 Facts, audit, fixes, Noa, activation | Facts entry, findings, fixes, reverts | 11.2, 11.8, AC-11.8.1; **AC-11.8.2 (new):** revert | 11 |
| Stats and Ads uploads, listing checks (FEATURE_BACKLOG) | Upload CSVs; deterministic findings | **AC-11.8.3, 11.8.4 (new)** | 11 |
| 12.1–12.8 Product Studio pipeline | Every stage of a product | 12.9; **12.9a (new):** product page panels | 12 |
| Shop structure writes (W2, W3) | Shipping, return and processing profiles; deletes | **12.10 (new)** | 12 |
| Return-policy, partner, digital-file, translation checks (FEATURE_BACKLOG) | Catalog checks | **12.11 (new)** | 12 |
| 13.1–13.3 Suppliers, quote requests, hard rules | Suppliers, requests, refused quotes | **13.2a, 13.3a (new)** | 13 |
| 13.5 Inventory and price monitoring | Monitored mappings | **13.5a (new)** | 13 |
| 13.6, 13.7 Ledger and P&L | P&L views | 13.7; **13.7a to 13.7d (new):** order profit and fee share, sales insights, accountant report and exports, payout matching | 13 |
| 13.8 Roni | The narrative | **13.8a (new)** | 13 |
| 13.10 Money actions | Refunds, cancellations, coupons, sales, Ads; statements | **13.10a (new)** | 13 |
| 14.1–14.5 Email, Dana, Etsy replies | Quarantine, drafts and send, delivery A or B | 14.6; **14.6a to 14.6c (new)** | 14 |
| Cases and review replies (FEATURE_BACKLOG) | Case tasks, drafted review replies | **14.6d (new)** | 14 |
| 15.2–15.10 The living office | Office, profiles, automation and tool centers, observatory, console, chat, activity | 15.3 to 15.9; **15.3a, 15.8a, 15.9a, 15.10a (new)** | 15 |
| 16.4, 16.5 Android approvals, office summary | The same as the web | **16.4a, 16.5a (new):** web parity first | 16 |
| 17.5 Windows Engineering Console | The same as the web | **17.5a (new):** web parity first | 17 |
| 18.1, 18.2, 18.5 FinOps, proposals, measurement | Unit costs, decisions, before and after | **AC-18.1.3, 18.2.3, 18.5.3 (new)** | 18 |
| 19.1, 19.3, 19.6 Engineering requests, Alex | Requests and their links | **19.1a (new)** | 19 |
| 19.5 Amir | Debug reports, remediations | **19.5a (new)** | 19 |
| 19.8 Engineering Console actions | Actions | 19.8 | 19 |
| 20.4 Runbooks and SLOs | Runbook links, SLO status | **20.4a (new)** | 20 |
| 20.6 Privacy operations | Data requests, retention runs | **20.6a (new):** Admin › Privacy | 20 |
| 21.1–21.4 Growth, experiments, multi-shop, Shopify | Analyses, experiments, comparison, a second marketplace | **21.1a to 21.4a (new)** | 21 |

## FEATURE_BACKLOG

Every user-facing row of [FEATURE_BACKLOG](../product/FEATURE_BACKLOG.md) now maps to a web step in the tables above:

| Backlog area | Web step |
|---|---|
| Shop stats, Etsy Ads results | AC-8A.5.4 (8A), AC-11.8.3 (11) |
| Buyer messages, cases, review replies, low-review drafts | 14.6b to 14.6d |
| Refunds, cancellations, coupons, sales, the monthly statement | 13.10a |
| Tracking where the API is blocked, AutoVA tracking | 6.10, AC-6.10.5 |
| Profit per order, fee share, forecasts, sale before and after, accountant report, payout matching | AC-8A.10.6, 13.7a to 13.7d |
| Ship-by alerts, holiday calendar, buyer countries | 6.5, AC-6.5.4, AC-6.9.3 |
| Refund reasons, repeat customers | AC-9.7.6, AC-10.7.4, AC-10.7.5 |
| Listing health checks, expiring listings, unintended edits, attributes, shipping profiles, translations, personalization, featured listings | AC-11.8.4 |
| Return policies, production partners, digital files | 12.11 |
| Morning summary channel | 15.9a |
| AutoVA quotes and order monitor | 7.16, AC-7.16.3, AC-8A.10.6 |
| Model mode, task difficulty | 9.8, 15.6 |
| Shop knowledge base | AC-9.10.13 |
| Vacation mode | 7.12, AC-7.12.3 |
| Out-of-stock reaction | 6.5, 7.5, 13.5a |
| Token and scope health | AC-4.4.2, AC-6.8.4 |
| Multi-shop comparison | 21.3a |
