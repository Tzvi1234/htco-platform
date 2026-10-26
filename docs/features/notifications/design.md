# Notifications — Design

| Layer | Module | What it holds |
|---|---|---|
| Domain | `htco_domain.notifications` | Kinds, audiences, email defaults, the parameter allowlist (`check_params`), preferences; severities (`severity_of`) and the channel rule (`delivery`, slice 7.6) |
| Application | `htco_application.notifications` | `NotificationService`, `EventDeliverer` with `IncidentNotifications` and `RequestedNotifications`, the `EmailPort` and its errors, the Hebrew and English templates (`render_email`), `EmailDeliverer`, `SecurityNotices` |
| Application | `htco_application.incident_center` | The inbox use cases (and the incident center's) |
| Application | `htco_application.notification_preferences` | A member's own preferences: read, and replace with the rules checked (slice 7.6) |
| Adapters | `htco_integrations.email` | `LogEmailAdapter` (default), `ResendEmailAdapter` (off) |
| Persistence | `htco_persistence.notifications` | Stores for the worker and the API |
| API | `htco_api.incidents`, `htco_api.notices` | `GET /v1/notifications`, `POST /v1/notifications/read`, `GET`/`PUT /v1/notifications/preferences` (slice 7.6); the security notices after sign-in, MFA enrollment, shop connect and disconnect |

## Data (`20261025110000_notifications.sql`)

- `app.notifications`: unique `(organization_id, user_id, dedup_key)`; `params` identifiers only (1 kB); `in_app` false when the recipient chose email only for that kind (the inbox leaves it out).
- `app.notification_settings` (language) and `app.notification_preferences` (in-app, email per kind): the user's own.
- `app.notification_severity_preferences` (in-app, email per severity) and `app.notification_shop_mutes` (slice 7.6, `20261027120000_notification_preferences.sql`): the user's own; a mute only for a shop the user can see (RLS on the insert).
- `app.email_deliveries`: sent, failed or skipped, with a status code; never the address or the body.
- `app.worker_notification_recipients(roles, user, shop)` and `app.worker_member_email(user)`: security definer, executable only by the worker, scoped to its job's organization.

## Flows

1. A source writes an event (`incidents.opened.v1` and `incidents.resolved.v1` both have a subscriber) in its own transaction (`incidents.opened.v1` with the incident; `notifications.requested.v1` from the API).
2. The outbox delivers it to `events.publish`; `EventDeliverer` hands it to its subscriber (idempotent).
3. `NotificationService` finds the recipients by audience (owners, owners and admins, or the user), with their preferences for the notification's kind, its severity and its shop, and applies `delivery` (slice 7.6): security kinds always go out on both channels; a muted shop stops its non-critical notifications; a critical one always reaches the inbox; otherwise a channel goes out when both the kind's and the severity's choice want it. It writes the rows and one `email.send` message per recipient, keyed `email:{dedup_key}:{user_id}`, in one transaction.
4. `email.send` renders the template, reads the address now, and sends with the outbox key as the provider's `Idempotency-Key`. 409, 429, 5xx and network errors are temporary (the outbox's backoff); other 4xx are permanent (recorded `failed`, not retried).

Security notices: a sign-in is "from a new device" when the user agent differs from every earlier session's (the first sign-in of an account is not); once per session. MFA enrollment, shop connected and disconnected go to the owners; re-authentication needed and refresh-token expiry (the worker's daily check) once per shop and ISO week.

## Preferences (slice 7.6)

| Severity | Kinds |
|---|---|
| critical | Every security kind; an incident whose own severity is critical |
| high | `incident_opened`, `incident_escalated` (unless they carry another), `alert_ship_by`, `relay_tracking_needed` |
| medium | `alert_low_review`, `alert_out_of_stock` |
| low | `incident_resolved` (unless it carries another) |

`PUT /v1/notifications/preferences` replaces the member's whole choice: anything left out returns to its default. It refuses (`422 preference-refused`, with the reason) a security kind, a critical severity without the inbox, and a muted shop that a shop-scoped member is not granted. `GET` lists every kind and severity with the defaults filled in, `locked` on the security kinds and `in_app_locked` on critical.
