# AutoVA quote mails (made-up fixtures, slice 7.15)

These mails are **invented** for the parser's golden-file tests (owner's decision, 2026-10-27): the
real layout is not known yet. When the owner sends real sample mails, add them here next to these,
with their expected results, and adjust the layout in `htco_application.quote_parser` if it
differs.

- Every address is `@example.com`; the AutoVA sender in the tests is `quotes@autova.example.com`,
  set as configuration (`HTCO_AUTOVA_QUOTE_SENDER`), never in the code.
- No real supplier, person, shop or listing: names, IDs and prices are made up.
- They live here, not under `tests/fixtures/`: that folder holds recorded HTTP responses, which
  must carry no email address at all (`test_fixture_hygiene.py`), while a mail has a sender by
  design. Every address here is on the reserved `example.com` domain.
- Each `*.eml.txt` is a plain-text mail: `From:`, `To:`, `DKIM-Signature:`, `Subject:`,
  `Message-ID:` and `Authentication-Results:` headers, a blank line, then the body. The receiving server in the tests
  is `mx.quotes.example.com` (`HTCO_AUTOVA_QUOTE_AUTHSERV_ID`); a mail is read only when that
  server's one, topmost Authentication-Results header reports `dkim=pass` for the sender's domain.
- `13` to `18` claim AutoVA in `From` without that proof (unsigned, no header, a forged header below
  ours, another server's header, a pass for another domain, a failed signature): each is
  quarantined before anything is read, and counted in the shop's forgery roll-up (one Human task
  per shop per day, slice 7.14).
- The `To:` is the owner's mailbox in the tests (`owner@shop.example.com`,
  `HTCO_AUTOVA_QUOTE_OWNER_MAILBOX`), and the made-up `DKIM-Signature:` (`d=autova.example.com`,
  `h=from:to:...`) covers it: the replay guard (#192) records a quote only then. The signature
  values are placeholders; our receiving server's verdict is the Authentication-Results header.
- `expected.json` holds, per file, the quote the parser must produce, or the reason it must refuse
  the mail (a refused mail opens one Human task "check this quote"; it is never guessed).
