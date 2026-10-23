# Notifications — Design

| Layer | Module | What it holds |
|---|---|---|
| Domain | `htco_domain.notifications` | Kinds, audiences, email defaults, the parameter allowlist (`check_params`), preferences |
| Application | `htco_application.notifications` | `NotificationService`, `EventDeliverer` with `IncidentNotifications` and `RequestedNotifications`, the `EmailPort` and its errors, the Hebrew and English templates (`render_email`), `EmailDeliverer`, `SecurityNotices` |
| Application | `htco_application.incident_center` | The inbox use cases (and the incident center's) |
| Adapters | `htco_integrations.email` | `LogEmailAdapter` (default), `ResendEmailAdapter` (off) |
| Persistence | `htco_persistence.notifications` | Stores for the worker and the API |
| API | `htco_api.incidents`, `htco_api.notices` | `GET /v1/notifications`, `POST /v1/notifications/read`; the security notices after sign-in, MFA enrollment, shop connect and disconnect |

## Data (`20261025110000_notifications.sql`)

- `app.notifications`: unique `(organization_id, user_id, dedup_key)`; `params` identifiers only (1 kB); `in_app` false when the recipient chose email only for that kind (the inbox leaves it out).
- `app.notification_settings` (language) and `app.notification_preferences` (in-app, email per kind): the user's own.
- `app.email_deliveries`: sent, failed or skipped, with a status code; never the address or the body.
- `app.worker_notification_recipients(roles, user, shop)` and `app.worker_member_email(user)`: security definer, executable only by the worker, scoped to its job's organization.

## Flows

1. A source writes an event (`incidents.opened.v1` and `incidents.resolved.v1` both have a subscriber) in its own transaction (`incidents.opened.v1` with the incident; `notifications.requested.v1` from the API).
2. The outbox delivers it to `events.publish`; `EventDeliverer` hands it to its subscriber (idempotent).
3. `NotificationService` finds the recipients by audience (owners, owners and admins, or the user), applies preferences (security emails cannot be turned off), and writes the rows and one `email.send` message per recipient, keyed `email:{dedup_key}:{user_id}`, in one transaction.
4. `email.send` renders the template, reads the address now, and sends with the outbox key as the provider's `Idempotency-Key`. 409, 429, 5xx and network errors are temporary (the outbox's backoff); other 4xx are permanent (recorded `failed`, not retried).

Security notices: a sign-in is "from a new device" when the user agent differs from every earlier session's (the first sign-in of an account is not); once per session. MFA enrollment, shop connected and disconnected go to the owners; re-authentication needed and refresh-token expiry (the worker's daily check) once per shop and ISO week.
