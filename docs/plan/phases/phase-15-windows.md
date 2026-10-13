# Phase 15 — Windows app

| | |
|---|---|
| **Milestone** | **M6 — Native apps** |
| **Repositories** | `htco-clients` |
| **Size** | L |
| **Depends on** | Phase 13 (can start earlier in parallel) |
| **Requirements** | FR-ADM-04, FR-IAM-06, FR-NTF-02 |
| **Status** | Not started — acceptance criteria refined at kickoff |

## Progress

- [ ] 15.1 RTL and navigation spike (NavigationView, title bar, flyouts in Hebrew)
- [ ] 15.2 Solution setup, view models in a plain library, MSTest, FlaUI smoke, CI
- [ ] 15.3 Auth, secure storage, session revocation
- [ ] 15.4 Orders grid, incident center, approvals
- [ ] 15.5 Developer console by role
- [ ] 15.6 Signing, packaging, updates

## Goal

A native Windows operations console for long working sessions: dense orders grid with bulk actions, incident center, approvals, and a developer console, built with C#/.NET 10 and WinUI 3, sharing tokens and contracts with the other clients.

## Before starting

The web app will already cover these screens. At kickoff, confirm the concrete reasons for a native Windows app (OS notifications, multi-window work, keyboard-heavy grids, offline read cache) so the scope targets what the web cannot do well. Three native clients cost roughly two to two and a half times the effort of web-only per feature.

## Scope

**In:** WinUI 3 on Windows App SDK 2.x; MVVM with CommunityToolkit.Mvvm and dependency injection; tokens as XAML theme dictionaries (light, dark, high contrast); Kiota-generated client behind an `IHtcoApi` facade; sign-in per the Phase 14 auth ADR (system browser with a loopback redirect and PKCE, or native screens); Windows Hello step-up with a device key verified by the server; tokens in Credential Locker (packaged) or DPAPI; orders grid, incident center, approvals, developer console by role; Windows notifications with deep links; RTL spike first; MSIX signed with Azure Artifact Signing; App Installer or WinGet distribution; automatic updates.

## Slices (outline)

| Slice | Content | Key acceptance |
|---|---|---|
| 15.1 | RTL and navigation spike (NavigationView, title bar, flyouts in Hebrew) | Decision recorded before building screens |
| 15.2 | Solution setup, view models in a plain library, MSTest, FlaUI smoke, CI | `ci-gate` green on `windows-latest` |
| 15.3 | Auth, secure storage, session revocation | No token in plain files or settings |
| 15.4 | Orders grid, incident center, approvals | Keyboard-only path through the main journeys; Narrator labels |
| 15.5 | Developer console by role | Masked traces; no free-form shell |
| 15.6 | Signing, packaging, updates | Signed MSIX installs and updates on a clean machine |

## Owner prerequisites

- Azure Artifact Signing account under the company (identity validation can take up to 20 business days; start early).

## Exit gate

- [ ] Signed release installed by the owner; acceptance scenario 5 (keyboard-only order to incident) passes.
