# ADR-0026: Browser Live View and Human Takeover over the session's own CDP pipe

- **Status:** Accepted (the owner approved the direction, 2026-10-26)
- **Date:** 2026-10-26
- **Deciders:** Owner, engineering
- **Extends:** [ADR-0025](0025-browser-and-network-providers.md):
  - its Connection layer gains a screencast and an input channel, inside the browser worker only (§1, §2);
  - its privacy baseline gains the rest of the Chromium privacy pack (§6).
- **Replaces:** the "noVNC or CDP screencast" choice left open in slice 8A.4's manual login. It is CDP screencast (§1). noVNC stays a documented option only (§5).
- **Related:**
  - [ADR-0006](0006-web-auth-bff.md): web sessions through the BFF;
  - [ADR-0019](0019-unified-execution-engine-and-action-gateway.md): the Human channel and approvals;
  - [ADR-0020](0020-per-shop-browser-environment.md): challenges go to a person, and the honest browser;
  - [ADR-0021](0021-authenticator-service.md): TOTP and the Secret Broker;
  - [Phase 8A](../plan/phases/phase-08a-browser-infrastructure.md): slice 8A.11.

## Context

People need to see what a browser session is doing, and sometimes to act in it. The cases are:
- supervision;
- a stuck step;
- a manual sign-in or MFA;
- a challenge that ADR-0020 sends to a person.

ADR-0025 sets the constraints:
- CDP runs only on the session's own pipe, and there is no TCP port anywhere.
- Sessions of different organizations share one browser worker.
- Browser work is dispatched, with a durable started marker before every write.
- Approvals and kill switches are checked at execution time.
- Chromium runs headless.

Live View and Takeover must keep every one of these.

## Decision drivers

- No CDP endpoint ever reaches a client or the network. The dashboard gets pictures and facts, and sends a closed set of typed input events.
- At most one actor drives a session at any moment: the agent or automation, or one person, never both.
- A takeover never writes. Every change to a shop goes through the Action Gateway, with its approval and its idempotency key. A takeover never causes a duplicate execution either, and it never stands in for an approval.
- No secret, and no typed keystroke, reaches a model, a log, an audit record or storage.
- It works in headless Chromium, so the browser worker needs no display server.

## Options considered

1. **CDP `Page.startScreencast` and `Input.dispatch*` over the session's pipe, relayed by the backend.** No new port, no new process, and it works headless (to be confirmed by the spike in §4). **Adopted.**
2. **Headed Chromium on Xvfb, with x11vnc, websockify and noVNC.** It shows the full desktop, but it adds a display server, a VNC server and a second remote-control path. Documented as a future option only (§5).
3. **A third-party remote browser view.** It would put the shops' signed-in sessions on someone else's service. Rejected.

## Decision

### 1. Live View

**The stream:**
- The session in the browser worker calls `Page.startScreencast` on its own CDP pipe, through Playwright's CDP session on that same connection. It acknowledges each `Page.screencastFrame` with `Page.screencastFrameAck`, which gives backpressure. Frames are JPEG, capped in size and in frame rate (for example a 1280-pixel width and 2 to 5 frames a second).
- The browser worker **dials out** to the API's internal Live View relay. It uses TLS on the private network, with a per-session worker token, and it is rate-limited. The browser worker therefore opens no listening port.
- The API fans the frames out to authorized viewers over an **authenticated WebSocket** on the dashboard's own origin. Opening it needs:
  - the BFF session, at `aal2`;
  - a **single-use viewing ticket** bound to the user, the organization, the shop and the browser session, which expires after 60 seconds;
  - an `Origin` check on the upgrade.

  A stolen or replayed ticket is refused.
- **What viewers see:**
  - the frames;
  - the facts, as labels that HTCO writes, never as text read from the page: the task status, the actor (which agent or automation), the shop, the recipe's label for the current step, the type of the last action, and each tab's normalized route.
- **Normalized routes, because titles and paths can hold buyer data** (names, usernames, order IDs, emails):
  - a tab's URL is shown as the recorded route pattern it matches, in the environment's path scope or the recipe's allowlist: for example `orders.detail` for `/your/orders/{order_id}`;
  - every dynamic segment is redacted, and the query string and fragment are dropped;
  - a URL that matches no recorded pattern shows only its site host and "unrecognized page".
- **Page-derived text**, a tab's title and its raw path without the query string, goes only to viewers with `VIEW_BUYER_CONTACT` on that shop. Like the frames, it is live and in memory only, and it is never stored or audited.

**No frames are stored.**
- By default, frames live only in memory, in the relay, for the moment they are delivered. Nothing writes a frame to a database, a disk or a log. There is no recording.
- Recording, if it is ever wanted, needs its own ADR: who can turn it on, its retention, and its redaction.

**Redaction:**
- **Buyer data, fail closed.** A recipe's declaration cannot guarantee what a signed-in page shows: a redirect, a notification or a layout change can bring buyer data onto it (ADR-0025 §5). So frames of **any signed-in page** go only to viewers with `VIEW_BUYER_CONTACT` on that shop, whatever the recipe declares. Other viewers get the facts and a "frames hidden: buyer data" placeholder.
- **Frames never go to a model.** They are for people only. During a takeover no model runs, and after it the agent sees the masked page text only, as ADR-0025 §5 rules for every screenshot.
- **Secrets.** The stream pauses whenever a secret is typed: by the session (a password from the Secret Broker, or a TOTP code from the Authenticator), or by a Controller during a takeover. The TOTP code view (8B) is never part of a session.
  - **A secret field** is a password or one-time-code field, by its type or its `autocomplete`. On a page that the path scope records as a verification page, every input counts, because a custom code widget may carry neither.
  - **The pause comes first.** The session pauses the stream before it types a secret. The input gate pauses it before it dispatches the first key or text event into a secret field. No frame is captured from then on.
  - **The cached frame goes too.** When the pause starts, the relay drops its latest frame and every frame still in flight, so neither a late frame nor a reconnect can show the field.
  - **It never resumes on blur**, because a code field can stay visibly filled after it loses focus. Frames resume only after a navigation to a new document, or once the controller has verified that the secret is no longer visible: the field is gone or empty, or it is a password field that is still masked. Until then every viewer, the Controller included, sees "paused: secret entry".

**Reconnect.** A dashboard that loses its connection asks for a fresh ticket and reconnects, and the relay resumes from the latest frame, or with none while the stream is paused for a secret. There is no history to replay. The relay keeps a heartbeat, and an idle viewer times out.

**Who can view.**
- **Holders:** a new capability, `VIEW_BROWSER_SESSION`. The Owner and Admins have it. Operations and Support have it on the shops they are granted.
- **Never:** agents and automations; they have no tool for it.
- **Limits:** a session has at most a small number of concurrent viewers, for example 3.
- **Audit:** every view's start and end is audited.

### 2. Takeover

**Two permissions and one Controller:**
- **The permissions.** `CONTROL_BROWSER_SESSION` is separate from viewing, and needs `aal2` and a step-up at each takeover.
  - **The Owner and Admins have it, limited by purpose** (D-25, the owner's decision of 2026-10-26).
    - **The Owner** may take control for any purpose.
    - **An Admin** may take control only for sign-in, MFA, a challenge or a stuck step, with no separate grant.
    - Either way, the takeover reaches only that purpose's pre-approved path scope (below). The pages blocked for every Controller stay blocked for the Owner too.
  - Agents and automations never have it.
- **The lease.** A session has **at most one Controller at a time**, through a lease: one active row per session in `browser_control_leases`, taken by compare-and-set.
  - It holds a short timeout, for example 90 seconds.
  - Only the holder can renew it, while connected, through its heartbeat.
  - It has a maximum total duration, for example 30 minutes, after which another step-up is needed.
- **A second request** while a lease is valid is refused, with the holder's name shown.

**What a takeover may do. It never writes.** A takeover gets a run past what blocks it. It never changes the shop:
- **sign-in, MFA and challenge:** completing the site's own sign-in, verification or challenge flow, in the page;
- **a stuck step:** unblocking the page without changing the shop, by navigating inside the scope, closing a dialog or fixing a form field;
- **the Owner's "other" purpose:** looking through the session inside its scope, read-only.

Every change to the shop, a submit included, goes through the Action Gateway. After Return Control, the run executes its own command under its approval and its idempotency key (below), and the person cannot send it from the takeover. D-25, who may take control, is unchanged: this limits only what control can do.

**Native dialogs are out of scope.** A WebAuthn or passkey prompt, like any other Chromium or OS dialog, is not part of the page. The screencast does not show it, and `Input.dispatch*` cannot drive it. A session that meets one stops as ADR-0020 rules (`LOGIN_REQUIRED`, with a Human task), and a takeover cannot complete it. Supporting one would need the full-desktop option of §5, under its own ADR.

**Take Control:**
1. The Controller asks to take control. The session pauses the agent or automation **at a safe point**:
   - a deterministic recipe, between two steps (the runner checks a pause flag before each step);
   - a Browser Use or LangGraph run, between two agent steps, at a checkpoint (an interrupt).
2. **What changes, and what stays held:**
   - **One record changes:** the browser session's `control_state`, which becomes `HUMAN_CONTROLLING`. Its transitions are in [execution-engine.md](../architecture/execution-engine.md) §7.
   - **The action stays `DISPATCHED`**, because its attempt is still running, so no new action, task or automation state is added. The status layer reports it as waiting, with a new waiting reason, `waiting_for_human_control`, in place of `waiting_for_browser`.
   - **What stays held:** the browser worker keeps the session open, and the browser, the CDP pipe, the **profile lock** and the job's lease all stay held.
   - **The deadline moves, within a bound.** The session's own deadline is suspended, up to the lease's maximum duration. Taking control records that bounded extension on the run's stored claim, in the same transaction as the lease, so ADR-0025's deadline watchdog moves with it. While a control lease is valid, the watchdog neither marks the run nor raises an incident.
3. **One input path, one actor at a time.** The session has a single **input gate**:
   - while `HUMAN_CONTROLLING`, it refuses every agent or automation action;
   - while the agent runs, it refuses every Controller event.

   Never both. This lives in one place, the `BrowserSession`, and is not left to convention.

**Input:**
- The dashboard sends a **closed set of typed events**: mouse moves, presses and releases, wheel scrolls, key presses and releases, and text insertion. Coordinates are relative to the frame.
- For every event, the API checks the schema, that the sender holds the lease, and the rate and size limits. It then relays the event to the browser worker.
- The `BrowserController` maps each event to `Input.dispatchMouseEvent`, `Input.dispatchKeyEvent` or `Input.insertText` on the pipe.
- No raw CDP command ever comes from a client: no `Runtime.evaluate`, no navigation command and no file access.
- **A path scope per purpose, deny by default.** A takeover can reach only what its purpose needs, through the same request interception as the payment block:
  - **sign-in, MFA and challenge:** only the site's sign-in, verification and challenge flows, listed in the environment's recorded **path scope** (for Etsy, the sign-in and two-step verification pages and the challenge pages; ADR-0022's scope table owns the list);
  - **stuck step:** only the current step's URL patterns, wherever the recipe declares pages per step. Otherwise it is that run's recipe allowlist;
  - **the Owner's "other" purpose:** an explicit deny-by-default scope, never open-ended. It covers the shop's Shop Manager paths that the environment records, minus every blocked page below;
  - **anything else**, a page outside the purpose's scope, is blocked and shown as blocked.
- **Pages blocked for every Controller, the Owner included.** These are the Owner-only W3 pages, the money pages and the account pages: refunds (B-09), coupons and sales (B-08), Etsy Ads (B-10), deleting or deactivating listings, payments, payouts and bank details, billing, the password and its reset, two-factor settings, connected apps and shop closure. A person who needs them uses Etsy directly, outside HTCO.
- **No write leaves the browser during a takeover.** This is enforced in the network layer, not by guessing what a click means. The same request interception refuses every request that could change the shop:
  - every method other than `GET` and `HEAD`, every form submission, and every WebSocket message that the page sends to the site;
  - except, in the sign-in, MFA and challenge purposes only, the flow's own requests to the endpoints that the path scope records as sign-in, verification or challenge endpoints;
  - and except a read that the site sends as a `POST`, when the path scope records that endpoint as read-only. The scope's review never records an endpoint that also writes.

  No scope lists a route where a `GET` changes state. A refused submit is shown to the Controller as "this change goes through HTCO". To make the change, the Controller returns control, and the change becomes a Gateway command: the run's own, or a new one under D-16. A W3 write still needs the Owner's press after a step-up.
- **Every page in the context is covered.** The path scope, the write block, the payment block and the screencast apply to every page of the browser context: a `target=_blank` link, a `window.open` popup and any other new page. A page they cannot cover is closed.
- **Navigation** stays inside that scope, and the person's clicks run under the same rules as the agent's:
  - the egress rules for Chromium's OS user still drop every private, loopback and link-local address (ADR-0025 §5, AC-8A.2.20);
  - during a takeover, payment pages stay blocked (D-24: HTCO never pays and never clicks Pay);
  - downloads, file choosers and clipboard reads are off.
- **The relay is not reachable from Chromium.** The browser worker's own process, a different OS user, dials out to the relay. A page in the session can never reach it.
- **Typed input is never kept.** Key and text values are never logged, audited, stored or shown to a model. Only counts are recorded.

**Return Control:**
1. **Releasing.** The Controller releases the lease. Expiry never returns control (see below).
2. **Re-checks before anything resumes:**
   - the page state: the recipe's expected state for its next step, the signed-in marker, and challenge detection;
   - the run's permissions;
   - the flags and the scope row;
   - every kill switch;
   - for every approval-gated run (every Etsy browser action, reads included, D-16), its approval, which must still be `APPROVED` for this request hash and not expired;
   - the deadline.

   A failed re-check ends the run safely, as `HANDED_TO_HUMAN` or `FAILED`. It never resumes.
3. **Resume from the checkpoint:**
   - A **deterministic automation** continues from its last completed step, after checking the next step's precondition. If the page matches no step, it stops safely.
   - A **LangGraph run** resumes from its interrupt. Its observation is the page as it is now ("a person acted; the current page is …"), through the normal masking, and never what the person typed.

**The write goes through the Gateway, once.** This ties into ADR-0025's started marker and run token:
- **The takeover happens inside the attempt that has already started.** The browser worker keeps the attempt's run token, the job's lease heartbeat and the profile lock, so the same attempt resumes after the return. No new attempt starts, and no other worker can be handed the binding.
- **The run makes the write, never the person.** After the return, the run executes its own command under its approval, re-checked at the return (D-16 for every Etsy action), and its idempotency key.
- **It sends only what was approved.** Before a write step, the run re-applies its command's values and re-reads what the submit would send. If anything else in it changed during the takeover, such as a form field the person fixed that the command does not set, the run does not submit. It goes to `HANDED_TO_HUMAN`, and the change, if it is wanted, becomes its own Gateway command, with its own approval and key.
- **A write step that was in progress, or whose effect is uncertain, is not run again after the return**, unless a re-read shows that it did not happen. If there is any doubt, the run goes to `HANDED_TO_HUMAN` for reconciliation.
- **A change the run did not make is an incident.** If verification still finds one, because the block failed or the site changed state on a read, the run does not write. It goes to `HANDED_TO_HUMAN`, and an incident opens, as for any verification mismatch. That change is never recorded as the run's result.
- **A takeover is not an approval.** Once control returns, every approval-gated action still needs its approval under the normal rules (D-16).
- **The person acts as themselves, and only reads.** During a takeover the person acts in the shop's signed-in session as themselves, inside the purpose's path scope and never on the blocked pages above. Apart from the auth flows' own requests, nothing they do sends a change. Every navigation and every refused request is audited under their name, and nothing they do there becomes an agent's or an automation's action.

**Audit.** Every takeover writes audit records for these events:
- the request, with its purpose: sign-in, MFA, challenge, stuck step or other;
- the grant, each renewal, and the release or expiry;
- each navigation, as its normalized route (§1);
- each refused request, as its normalized route and method, with a count;
- the number of input events;
- the result of the return checks.

The audit never holds a key or a text value, a page title, a raw path or a query string.

**When something fails:**
- **The Controller disconnects.**
  - The lease is not renewed, so it expires. The run **stays paused**, and it never goes back to the agent on its own, because the page may be half-changed.
  - Another Controller can take over, or the same one can return.
  - At the maximum duration, the run becomes `HANDED_TO_HUMAN` with a task. The session then closes cleanly, re-sealing the profile under the fence. A manual sign-in that succeeded is exactly what should persist.
- **The browser worker crashes.**
  - The pipe and the lock connection drop, Postgres releases the profile lock, and the workspace in tmpfs is lost. The last good archive stays, so a manual sign-in just before the crash is lost safely: the next session shows `LOGIN_REQUIRED`.
  - The attempt resolves through ADR-0025's started marker and watchdog: never a double write. The lease is ended, and viewers see "session ended".
- **The API's relay crashes.** Viewers and the Controller drop, and the lease expires. This is the same as a disconnect.

**No secrets reach the model.**
- During a takeover no model call runs, because the agent is paused, and no frame is sent to a model.
- After the return, the agent sees only the page through the normal masking (AC-8A.7.2).

### 3. Contracts

- **`LiveSession`** lives in the browser worker, on the Connection layer:
  - `start_screencast(limits)`, `stop_screencast()`, `pause_frames()` and `resume_frames()`;
  - `dispatch(event: ControllerInput)`, which takes the closed set only;
  - the input gate's state.
- **`BrowserTakeover`**, an application port, gives `request_control`, `renew`, `release`, `state` and `return_checks`. It is async, with leases in Postgres.
- The provider's capabilities declare `live_view` (screencast and input). The shared contract suite tests it, with the takeover's write block and the secret pause.

The signatures are in [execution-engine.md](../architecture/execution-engine.md) §3.

### 4. Headless: confirmed by a spike, not assumed

`Page.startScreencast` and the `Input` domain are CDP features that the browser serves without a display. The plan runs Chromium headless, in Playwright's new-headless `chromium` channel. **Slice 8A.11 begins with a spike** on the fleet's pinned build, over the pipe, against a `127.0.0.1` page. It must confirm:
- the frames arrive;
- clicks, typing (keys and `insertText`) and wheel scrolls reach the page;
- the managed policies of §6 apply to this build;
- WebSockets work end to end: the dashboard's socket through the API's production edge, and the browser worker's private dial-out to the relay from its own host.

The result goes in this ADR's validation log before the rest of 8A.11 is built. If the pinned headless build cannot deliver frames, the fallback is headed Chromium on Xvfb inside the same worker. That would be an amendment of this ADR, never a silent switch.

### 5. A future option, documented only: a full-desktop view

If a site ever needs what a page screencast cannot show, such as a native dialog (a WebAuthn or passkey prompt, §2), the option is:
- headed Chromium on Xvfb;
- x11vnc, bound to localhost inside the worker;
- websockify;
- noVNC behind the same authenticated relay, the same tickets and the same single Controller.

It is not adopted. It would need its own ADR and the same gates, and there would never be a public VNC port.

### 6. The rest of the Chromium privacy pack (extends ADR-0025 §2)

ADR-0025 already covers background networking, component updates, field trials, domain reliability, pings, and Safe Browsing off. This section adds the rest, through **managed enterprise policies**. They live in one policy file pinned in the browser-worker image, which a page or a session cannot change:

- **DNS over HTTPS with a resolver HTCO chooses:** `DnsOverHttpsMode = secure`, and `DnsOverHttpsTemplates` set to one resolver recorded in TOOLCHAIN, written as an IP literal so that no system resolver is needed to reach it. There is no fallback to plain DNS. Chromium's OS user has no plain DNS at all, so the egress rules of ADR-0025 §5 never break name resolution (AC-8A.2.27).
- **No extensions:** `ExtensionInstallBlocklist = ["*"]`.
- **Trackers:**
  - `BlockThirdPartyCookies`, unless a recorded recipe needs a third party;
  - the Privacy Sandbox ad features off;
  - a pinned tracker blocklist in `URLBlocklist`, tested against every recipe, so a sign-in or a challenge page still works.
- **No sync, no reports:** `SyncDisabled`, `MetricsReportingEnabled = false`, `UrlKeyedAnonymizedDataCollectionEnabled = false`, and no crash reports.
- **No helpers that send data out:** search suggestions, spell-check service, translation, network prediction, password manager and autofill are all off. Geolocation and notifications default to blocked.
- **Fail closed.** At startup and before each session, the browser worker reads the effective policies (`chrome://policy`) and compares them with the pinned file. A missing or changed policy starts no session and reports `policies_missing`. This mirrors the egress-rule check of ADR-0025 (AC-8A.2.22).
- **Per-session cleanup.** At the end of a session, before the fenced re-seal, everything that does not need to persist is cleared:
  - cache, history, downloads, crash dumps and temporary files;
  - the storage and service workers of origins outside the environment's recorded site origins (the origins of its path scope and its recipes' allowlists).

  The archive keeps only the allowlisted origins' cookies and storage, which hold the sign-in.

Brave is not adopted (owner, 2026-10-26: not approved).

## Consequences

- **Positive.**
  - People see and fix a stuck session, a sign-in or an MFA prompt without leaving HTCO, through the same lock, fence, approvals and audit.
  - There is no new port, no display server, and no third party holding a signed-in session.
- **Negative.**
  - A new real-time path, the relay and its WebSocket, becomes a high-value target. Tickets, the origin check and per-event lease checks guard it.
  - Pixels cannot be redacted line by line. Buyer-data pages are hidden from viewers without the right, and the stream pauses during secrets.
  - A paused run holds a browser slot and a profile lock for up to the takeover's maximum duration.
  - A takeover cannot make a change itself. A stuck write waits for the return, and any other change the person wants becomes its own Gateway command.
- **Follow-ups:**
  - the 8A.11 spike (§4);
  - the Live View screen in `htco-clients`, web first (ADR-0016);
  - the choice of DoH resolver, recorded in TOOLCHAIN.

## Validation

The acceptance criteria are AC-8A.11.1 to AC-8A.11.21 in Phase 8A.

## Validation log

- 2026-10-26: The owner approved the direction: Live View and Takeover over CDP screencast, a single leased Controller, no frames stored, headless confirmed by a spike, noVNC documented only, and the privacy pack without Brave. Written after ADR-0025's last review round, so it adopts that round's fail-closed rules from the start: frames of signed-in pages only for buyer-data viewers, no frame to a model, the private-network rule, D-16 approvals for every browser action, and the run token.
- 2026-10-26: The security review of #166 (sage-arch) narrowed the takeover. There is now a deny-by-default path scope per purpose. The Owner-only W3, money and account pages are blocked for every Controller. Control was proposed as Owner-only by default; the owner then decided D-25 (Admins limited): the Owner for any purpose, an Admin only for sign-in, MFA, challenge or a stuck step, both inside the purpose's path scope, with no separate grant. The undefined "site allowlist" is replaced by the environment's recorded path scope and its recipes' allowlists.
- 2026-10-26: Codex's review of #166 at `ba922b8` (local, posted by harbor) found three P1s and a P2. They are fixed here, not deferred:
  - **A takeover never writes.** Every request that could change the shop is refused at the context's interception, apart from the auth flows' own. The write is the run's own Gateway command after the return, and it sends only what was approved. D-25, who may take control, is unchanged.
  - **No buyer data in facts or audits.** Both use HTCO's labels and normalized routes. Titles and raw paths go only to buyer-data viewers, and are never stored.
  - **The secret pause holds.** It starts before the first key, drops the cached frame, and ends only after a navigation or a verified mask, never on blur.
  - **Native WebAuthn and passkey prompts are out of scope** for a takeover (AC-8A.11.6).

  The later notes of quill and sage-arch are folded in too: one status vocabulary, the watchdog's extension on the claim, new pages covered, and the spike's WebSocket checks (AC-8A.11.12, 11.15, 11.16 and 11.18 to 11.21).
- 2026-10-27: Codex's review of `c12502b` (local, posted by harbor) raised four P1s and a P2. Under the last-rewrite rule (main), each was answered on #166 with a rationale. None changes this decision: each tightens how a rule above is enforced. They became implementation criteria AC-8A.11.22 to 11.26 in Phase 8A, which extend this ADR's list:
  - the write block covers every channel: service workers, sockets opened earlier, beacons, and anything it cannot see turned off;
  - control is bound to the session's organization and shop;
  - validation never echoes input;
  - revocation takes effect at once;
  - one immutable deadline per takeover episode. A retake after Return Control, in the same run, is a new episode with its own deadline, and a run has at most 3 episodes, after which it goes to `HANDED_TO_HUMAN` or fails (main's decision). This settles the maximum duration in §2: "another step-up" never extends an episode.
