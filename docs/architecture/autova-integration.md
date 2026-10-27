# AutoVA integration: repeat orders, new-product quotes and tracking

| | |
|---|---|
| **Status** | 1.0 (owner's flow, 2026-10-23) |
| **AutoVA** | The owner's supplier platform at `beta.autova.io` ("AutoVA (Beta)"). It is connected to Etsy by itself. It has no public API (V-08, A-06) |
| **Related** | [Phase 6 slice 6.10](../plan/phases/phase-06-fulfillment-incidents.md#610--autova-tracking-relay) (tracking relay), [Phase 7 slices 7.13–7.17](../plan/phases/phase-07-tasks-approvals.md#713--supplier-and-quote-model) (quotes), [Phase 8A slice 8A.10](../plan/phases/phase-08a-browser-infrastructure.md#8a10--autova-recipes-a-01-export-first-and-a-02-approve-the-chosen-quote) (AutoVA recipes), [Phase 13](../plan/phases/phase-13-suppliers-finance.md) (supplier evaluation and finance), [ADR-0019](../adr/0019-unified-execution-engine-and-action-gateway.md), [ADR-0020](../adr/0020-per-shop-browser-environment.md) |

## The money rule

**Payment is AutoVA's own Auto Pay**, which the owner switches on in AutoVA. **HTCO never pays, and never clicks Pay.**

- No recipe has a payment control in its allowed actions. A page that shows a payment step stops the run and hands it to the owner.
- Choosing a quote is the owner's decision every time. It is an Owner-only approval (policy W3) with step-up.
- An order that stays **Unpaid** past its window (for example, Auto Pay is off or a card failed) raises one alert and a Human task for the owner. HTCO does nothing else.

## Flow 1: a repeat order (the product already has a supplier mapping)

1. An Etsy order arrives. AutoVA creates the supplier order on its own; HTCO does nothing here.
2. AutoVA pays through Auto Pay.
3. **HTCO watches the order's status** through the AutoVA Orders data: recipe A-01, Export first, then a screen read as the fallback.
4. When AutoVA shows the shipping company and tracking number, the **tracking relay** (slice 6.10) copies them and uploads them to Etsy (API first; otherwise B-12 or a Human task).
5. **Eitan** supervises the exceptions from Phase 9: stuck orders, missing tracking, profit anomalies.

## Flow 2: the first order of a new product

1. AutoVA requests quotes from suppliers on its own.
2. Every quote arrives **by email**, and HTCO listens to that mailbox (D-23).
3. Each quote appears in HTCO at once, under its product: the supplier, price, shipping terms and shipping times, from a **deterministic parser with no model** (slice 7.15).
4. The owner compares the quotes on the "New product quotes" screen and picks one. This needs the owner's approval every time (slice 7.17).
5. An automation approves **that** quote in AutoVA, confirms the order, and records the supplier mapping:
   - from Phase 8A, recipe A-02;
   - until then, a Human task with the chosen quote and a link.

   The mapping becomes active only after the AutoVA order is re-read as confirmed.
6. From then on, every order of that product goes to that supplier automatically through AutoVA (flow 1).

## The AutoVA Orders data

Each order row on the Orders page shows the fields below. The same fields are expected in the Export file, to be confirmed with a real file at the start of slice 8A.10.

| AutoVA field | HTCO field (`autova_orders`) | Use |
|---|---|---|
| AutoVA ID | `autova_order_id` | Key, together with the shop |
| Etsy receipt number | `receipt_id` | Join to `etsy_orders` |
| Date | `ordered_at` | Age, SLAs |
| Supplier | `supplier_name` → `supplier_id` | Mapping check |
| Status: Unpaid / Confirmed / Tracking / Shipped | `status` (`unpaid`, `confirmed`, `tracking`, `shipped`) | Order monitor; Unpaid alert |
| Paid / Unpaid | `paid` | Auto Pay check |
| "Semi Auto" | `mode` | Shows whether AutoVA placed the order automatically |
| Shipping company (for example Yun Express) | `carrier` | Tracking relay (carrier mapped to Etsy's carrier list, `etsy.shipping_carriers.read`) |
| Tracking number | `tracking_code` | Tracking relay |
| Profit | `autova_profit` (Decimal, currency) | Profit per order, cross-checked with the Etsy ledger (Phase 13) |

Each snapshot keeps `source` (`autova_export` or `autova_screen`), `observed_at` and the recipe version. A status may only move forward: Unpaid → Confirmed → Tracking → Shipped. A backward move is stored as a drift and raises a finding, never applied silently.

## Recipes (AutoVA site)

AutoVA is not Etsy, so the Etsy approval page does not govern it. These recipes run in the owner's own AutoVA account, through an AutoVA Browser Environment (ADR-0020), only within **AutoVA's terms (A-08, still to be checked)**. Each recipe has its own flag, off until the owner confirms the terms allow it.

| ID | Recipe | Kind | Approval in HTCO | Phase |
|---|---|---|---|---|
| A-01 | Read order status, carrier, tracking number and profit: **Export first** (download, then parse and validate), a **screen read** of the Orders page as the fallback | Read | None per run, on a schedule; audited | 8A |
| A-02 | Approve the owner's chosen quote and confirm the order. Never Pay | Write (commits a supplier order) | W3: the owner's approval of that quote, each time; the request hash must match the approved quote | 8A |

AutoVA signs in with email and password and has no TOTP (A-08, 2026-10-23). The password is sealed in the vault and released by the Secret Broker (slice 5.17) to the browser worker only. The Authenticator (Phase 8B) is not needed for AutoVA.

## The quote mailbox (D-23)

| Option | How | Pros | Cons |
|---|---|---|---|
| **(a) Forwarding rule (recommended)** | A rule in the owner's mailbox forwards **only** AutoVA's quote mails to `quotes@office.ht-co.app`, which is Resend inbound with MX on `office.ht-co.app`. Signed webhook into the inbox (slice 5.2) | HTCO sees only AutoVA mail; no OAuth to the owner's mailbox; the same path Phase 14 uses | Needs the MX records and the rule. Forwarded mail must be authenticated by sender plus a shared secret in the address (for example `quotes+<token>@`) |
| (b) Gmail read-only OAuth | `gmail.readonly` with a query filter on AutoVA's sender, polled every few minutes | No DNS change | HTCO holds a token able to read the whole mailbox; Google's verification for a restricted scope |

AutoVA's sending address is still unknown (A-09). The parser accepts only mail from the confirmed sender, and anything else is quarantined.

**The sender is authenticated, never taken from `From`.** Anyone can write `From: <AutoVA's address>`, so a quote mail is read only when our receiving server vouches for it (`dkim_aligned` in `htco_application.quote_parser`, RFC 8601):

1. Only the Authentication-Results header whose authserv-id is our receiving server's counts (`HTCO_AUTOVA_QUOTE_AUTHSERV_ID`, configuration like the sender).
2. It must be the topmost one, the one our server added on arrival, and the only one with our ID: a second one was written by the sender, so the mail fails.
3. It must report `dkim=pass` with `header.d` (or else the domain of `header.i`) equal to the configured sender's domain: strict DMARC alignment. DKIM survives the mailbox's forwarding (D-23 (a)); SPF does not, so SPF and DMARC results alone never count.
4. A header that is missing, unreadable (quoted strings, unbalanced comments) or ambiguous fails closed.

Order in the parser: a mail from another `From` is quarantined with no task; a mail whose `From` is AutoVA's but fails the verdict is quarantined and counted in the shop's forgery roll-up (below). Both happen before the body is read, and nothing is recorded.

## The quote mailbox intake (slice 7.14)

The owner chose option (a): a Gmail rule forwards AutoVA's quote mails to Resend inbound.

```
owner's Gmail --forward--> quotes+<token>@office.ht-co.app (Resend inbound, MX)
  --> POST /webhooks/autova-quotes   (Svix-signed email.received notice: metadata only)
        verify signature -> check the address -> jobs.inbox (source autova_quotes,
        event_id = Message-ID) + job autova.quote_mail, one transaction
  --> worker: GET /emails/receiving/{id} -> raw.download_url (HTTPS, <= 1 MiB)
        -> QuoteMail.from_text (our server's DKIM verdict, the signed To) -> QuoteIntake (7.15)
```

- **Signature.** Resend signs with Svix (`svix-id`, `svix-timestamp`, `svix-signature`), the Standard Webhooks scheme of slice 5.3 under other header names, with its own `whsec_` secret (`HTCO_AUTOVA_QUOTE_WEBHOOK_SECRET`). Unsigned, wrongly signed or outside the 5-minute window: 401 (AC-7.14.3).
- **The address carries a secret token.** `HTCO_AUTOVA_QUOTE_MAILBOX` is `quotes+<16 or more letters or digits>@…`, a secret. A notice whose `to` or `received_for` does not name it is kept with no job and counted (`not_our_mailbox`): mail sent straight to the domain by anyone who does not know the token is never parsed, and cannot take a Message-ID ahead of AutoVA's mail.
- **Once.** The inbox's `event_id` is the mail's Message-ID, which Gmail's forwarding keeps, so the same mail delivered or forwarded twice is one row and one job (AC-7.14.1). Only Resend's email ID and the Message-ID are stored; the mail (DC2) is fetched by the job and never stored, logged or read by a model.
- **The shop.** `HTCO_AUTOVA_QUOTE_ETSY_SHOP_ID` names the Etsy shop; its connected HTCO shop gets the quotes (the 5.3 lookup). Not connected: kept with no job (`unknown_shop`).
- **The worker settings** stay in configuration: `HTCO_AUTOVA_QUOTE_SENDER`, `HTCO_AUTOVA_QUOTE_AUTHSERV_ID`, `HTCO_AUTOVA_QUOTE_OWNER_MAILBOX` and `HTCO_RESEND_API_KEY`. The `autova.quote_mail` handler (`quote_mail_handlers`) is registered once 7.1's task store backs the Human tasks; until then the jobs wait in the queue.

### The receiver's authserv-id (#192.1): not confirmed yet

Resend's documentation (read 2026-10-27) does not say whether inbound mail gets an `Authentication-Results` header, or under which authserv-id. The received-email API returns the receiver's verdict in a separate `authentication` object (`spf`, `dkim`, `dmarc`) whose format is not documented, and the example `headers` hold no `Authentication-Results`. So the ID cannot be confirmed from the documentation. **`HTCO_AUTOVA_QUOTE_AUTHSERV_ID` stays unset until it is confirmed from a real mail**, and until then `dkim_aligned` fails closed and every quote mail is quarantined.

How to confirm it, once the MX records and the forwarding rule exist: forward one real AutoVA quote mail, download its raw file (`GET /emails/receiving/{id}`, then `raw.download_url`), and read the topmost `Authentication-Results` header. Record the authserv-id, the date and the mail's Message-ID here, then set the variable.

- If Resend adds its own header (topmost, with its own ID), that ID is the one to configure.
- If the topmost one is Gmail's (`mx.google.com`), written when the mail reached the owner's mailbox, it may be configured only because the address token keeps everyone but the owner's forwarding out: anyone who could write to the address directly could also write that header.
- If there is none, the intake stays off. The fallback is to read Resend's `authentication` object instead, once its format is confirmed. That is a separate change with its own tests.

### Forgeries roll up (#192.2)

A mail whose `From` names AutoVA but fails the verdict is counted in **one Human task per shop per day** (`unauthenticated_sender`, carrying the count; `QuoteReviews.possible_forgery`), not one task per mail, so a forgery flood cannot bury the queue. Nothing of the mail is kept. The day is the UTC date.

### DKIM replay (#192.3)

A genuine AutoVA mail, validly signed, could be replayed to our mailbox, for example another AutoVA customer's quote. A parsed quote is recorded only when **the `To` covered by AutoVA's DKIM signature is the owner's mailbox** (`HTCO_AUTOVA_QUOTE_OWNER_MAILBOX`, `signed_recipients`):

1. every `DKIM-Signature` reads, and exactly one has `d=` AutoVA's domain;
2. its `h=` lists `from` and `to`;
3. the mail has exactly one `To` header. A verifier checks only the bottom-most instance of a signed header, so a second `To` added on top would be unsigned.

Otherwise the intake opens one Human task "check this quote" with the reason `unknown_listing` and records nothing. The other option #192 allows (the listing belongs to the target shop) needs a listings catalogue that HTCO does not keep yet; it can be added as a second way to pass later.

## The quote mail's layout (slice 7.15)

The parser (`htco_application.quote_parser`) reads one layout: `Label: value` lines, in English or Hebrew, with no model involved. Until the owner sends real mails, the layout is **made up** (the fixtures in `tests/application/quote_mails/`); a real mail that differs is refused and opens one Human task, never a guessed quote, and the layout is adjusted with its own golden file.

| Field | English label | Hebrew label | Required | Reads |
|---|---|---|---|---|
| AutoVA product | Product | מוצר | Yes | `AV-…` code |
| Etsy listing | Etsy listing | רישום Etsy | Yes | digits |
| SKU | SKU | מק״ט | No | code |
| Variant | Variant | וריאציה | No | code (for example `pid:5512`) |
| Supplier | Supplier | ספק | Yes | the name AutoVA shows |
| Unit price | Unit price | מחיר ליחידה | Yes | `7.40 USD`, `USD 7.40`, `$1,240.00`, `€9.15`, `£4.80`, `₪ 38.50` |
| Shipping | Shipping | משלוח | Yes | money in the same currency, or Free / חינם |
| Shipping method | Shipping method | שיטת משלוח | Yes | text |
| Processing time | Processing time | זמן טיפול | Yes | `2-4 days`, `1 day`, `2–4 ימים` |
| Shipping time | Shipping time | זמן משלוח | Yes | the same |
| Valid until | Valid until | בתוקף עד | No | ISO date |
| Quote ID | Quote ID | מזהה הצעה | Yes | `Q-88213` (the quote's source reference) |

Refused: an unknown label, a field twice, a required field missing, a value that does not read exactly, a price and shipping in two currencies, or a mail over 200,000 characters.
