# Phase 17 — Windows app

| | |
|---|---|
| **Milestone** | **M6 — Native apps** |
| **Repositories** | `htco-clients` |
| **Size** | L |
| **Depends on** | [Phase 15](phase-15-office-executive.md) (can run in parallel with Phases 18–19) |
| **Requirements** | FR-ADM-04, FR-IAM-06, FR-NTF-02 |
| **Status** | Not started |

Rolling wave: this phase is planned at the level of scope, slices and gates; acceptance criteria are refined at kickoff. The "key acceptance" column below is the minimum.

## Progress

- [ ] 17.1 RTL and navigation spike (NavigationView, title bar, flyouts in Hebrew)
- [ ] 17.2 Solution setup, view models in a plain library, MSTest, FlaUI smoke, CI
- [ ] 17.3 Auth, secure storage, session revocation
- [ ] 17.4 Orders grid, incident center, approvals
- [ ] 17.5 Engineering Console by role
- [ ] 17.6 Signing, packaging, updates

## Goal

A native Windows operations console for long working sessions: dense orders grid with bulk actions, incident center, approvals, and the Engineering Console (engineering requests, incidents, dead letters, optimization proposals, pull requests, deployments and infrastructure health), built with C#/.NET 10 and WinUI 3, sharing tokens and contracts with the other clients.

## Before starting

The web app will already cover these screens, including the office of [Phase 15](phase-15-office-executive.md). At kickoff, confirm the concrete reasons for a native Windows app (OS notifications, multi-window work, keyboard-heavy grids, offline read cache) so the scope targets what the web cannot do well. Three native clients cost roughly two to two and a half times the effort of web-only per feature.

## Scope

**In:** WinUI 3 on Windows App SDK 2.x; MVVM with CommunityToolkit.Mvvm and dependency injection; tokens as XAML theme dictionaries (light, dark, high contrast); Kiota-generated client behind an `IHtcoApi` facade; sign-in per the [Phase 16](phase-16-android.md) auth ADR (system browser with a loopback redirect and PKCE, or native screens); Windows Hello step-up with a device key verified by the server; tokens in Credential Locker (packaged) or DPAPI; orders grid, incident center, approvals; the Engineering Console by role (read-only views as in Phase 15; the safe actions of [Phase 19](phase-19-engineering.md), such as approving a release gate or retrying a dead letter, once they exist, always through the API's checks); Windows notifications with deep links; RTL spike first; MSIX signed with Azure Artifact Signing; App Installer or WinGet distribution; automatic updates.

## Owner prerequisites

- Azure Artifact Signing account under the company (identity validation can take up to 20 business days; start early).

## Slices (outline)

| Slice | Content | Key acceptance |
|---|---|---|
| 17.1 | RTL and navigation spike (NavigationView, title bar, flyouts in Hebrew) | Decision recorded before building screens |
| 17.2 | Solution setup, view models in a plain library, MSTest, FlaUI smoke, CI | `ci-gate` green on `windows-latest` |
| 17.3 | Auth, secure storage, session revocation | No token in plain files or settings |
| 17.4 | Orders grid, incident center, approvals | Keyboard-only path through the main journeys; Narrator labels |
| 17.5 | Engineering Console by role | Masked traces; no free-form shell; every action goes through the API and is audited |
| 17.6 | Signing, packaging, updates | Signed MSIX installs and updates on a clean machine |

## Exit gate

- [ ] Signed release installed by the owner; acceptance scenario 5 (keyboard-only order to incident) passes.

## Cost impact

Windows code signing about $10/month ([COST_MODEL](../../cost/COST_MODEL.md)).
