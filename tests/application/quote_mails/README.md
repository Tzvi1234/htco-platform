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
- Each `*.eml.txt` is a plain-text mail: `From:`, `Subject:` and `Message-ID:` headers, a blank
  line, then the body.
- `expected.json` holds, per file, the quote the parser must produce, or the reason it must refuse
  the mail (a refused mail opens one Human task "check this quote"; it is never guessed).
