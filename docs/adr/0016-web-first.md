# ADR-0016: Web first — every feature works on the web before any native app

- **Status:** Accepted
- **Date:** 2026-10-14
- **Deciders:** Owner
- **Related:** [ADR-0001](0001-two-repositories.md), [PLAN](../plan/PLAN.md) (Phases 16 and 17), D-05

## Context

HTCO OS will have three clients: the web app, a Windows app and an Android app. The owner wants all three, and native apps matter. Building every feature three times at once would slow everything down, and a feature that exists only in a native app would leave the web, the one client every user can open anywhere, incomplete.

## Decision

1. **The web app is the primary client.** Every feature and every system (operations, approvals, the office, settings, administration) ships first and fully on the web.
2. **Native apps follow, never lead.** The Windows and Android apps implement features the web already has, against the same API. No feature may exist only in a native app.
3. **One API for all clients.** Business rules live in the platform, never in a client, so native apps add presentation and device integration (notifications, biometrics, offline cache), not new behaviour.
4. **Native-only capabilities are the exception**, limited to what the device itself provides (push notifications, biometric step-up, background sync); each still has a web counterpart where one is possible (for example, web notifications or a TOTP step-up).

## Consequences

- Phase order is unchanged: the web shell and screens come in Phase 3 and grow with every later phase; Android (Phase 16) and Windows (Phase 17) come after the operational core, and their parallel tracks start only for features the web already has.
- A pull request that adds a user-facing feature includes its web screen, or names the web pull request that does.
- Acceptance criteria of user-facing slices are verified on the web; native apps get their own criteria when they implement the feature.
