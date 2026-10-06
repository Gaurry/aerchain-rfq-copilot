# RFQ Copilot — Kill the Quote Spreadsheet

A prototype that drafts an RFQ, reads whatever vendors send back (Excel, PDF, Word, a phone photo, a one-line email), lands every quote in one side-by-side comparison, and lets a buyer question the result in plain language.

Built for the Aerchain product take-home.

**Category:** corrugated packaging, 30 line items, 5 vendors, ~₹4.1 crore annual spend.

**Core principle:** AI reads and interprets. Code calculates. Humans decide.

_Work in progress._

## Layout

```
app.py              Streamlit UI
core/rfq.py         30-line RFQ, last-year prices, questionnaire, assumptions
core/schema.py      What the AI must return (literal values + evidence)
core/matching.py    Vendor item → RFQ line (code, spec, category rules)
core/normalize.py   Deterministic ₹/pc landed ex-GST, with a step ledger
data/inbox/         Vendor responses as received (email is stubbed)
evals/answer_key.xlsx  Ground truth for scoring extraction
tests/              Golden fixtures (tests only) + normalization checks
```

Run tests: `python -m tests.test_normalize`
