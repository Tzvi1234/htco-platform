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
