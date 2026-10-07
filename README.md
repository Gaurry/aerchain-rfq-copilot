# RFQ Copilot — Kill the Quote Spreadsheet

A prototype for the Aerchain take-home. A buyer talks an RFQ into existence, vendors reply however they like (Excel, letterhead PDF, Word, a phone photo, a one-line email), the AI reads every reply, and the buyer reviews what it isn't sure about, compares like for like, asks questions in plain language, and exports an award.

**Category:** corrugated packaging · 30 lines · 5 vendors · about ₹4.1 crore a year.
**Rule:** AI reads and interprets. Code calculates. Humans decide.

## Run it

```bash
pip install -r requirements.txt
export ANTHROPIC_API_KEY=sk-ant-...      # or put it in .streamlit/secrets.toml
streamlit run app.py
```

On Streamlit Community Cloud, add `ANTHROPIC_API_KEY = "sk-ant-..."` under the app's **Settings → Secrets**.
Without a key, **Skip to the finished example** still works (it loads replies read earlier by the same pipeline); drafting, live reading and questions need the key.

**Voice:** the 🎙️ Speak button uses the browser's speech recognition. Use Chrome or Edge and allow the microphone.

## The flow

1. **Draft RFQ** — speak or type to the co-pilot ("same boxes as last year, bump the atta shippers by 20%").
2. **Send** — the exact email/WhatsApp each vendor gets, with the RFQ Excel attached (sending is stubbed).
3. **Responses** — each reply as received; **Read all 5 replies with AI** (about a minute). Add any other vendor's file here.
4. **Review** — only what the AI isn't sure about: approve, correct, exclude, or ask the vendor (AI drafts the email).
5. **Compare** — ₹ per piece delivered to Bawal before GST, L1 per line, "Not quoted" never counted as zero; click a price for its working.
6. **Award** — recommendation, what fixing a conditional vendor would save, conditions before signing, Excel export.

**Reset:** refresh the page. Each session starts clean; the saved vendor reads stay.

## Layout

```
app.py                 Streamlit UI (one screen per step, co-pilot in the sidebar)
core/readers.py        Any file → content the model can read
core/extract.py        Live extraction with Claude (literal values + evidence) and saving
core/ai_match.py       AI matching for items described only in words (rules accept/reject)
core/matching.py       Vendor item → RFQ line (code, size/ply, category and "rest" rules)
core/normalize.py      Deterministic ₹/pc landed ex-GST, with a step-by-step ledger
core/quality.py        Questionnaire gate (cleared / conditional / failed / no questionnaire)
core/compare.py        One comparison used by every screen and the analyst
core/copilot.py        RFQ drafting through explicit edit tools
core/analyst.py        Questions answered by tools over the comparison (no mental maths)
prompts/               Every instruction the model gets, as plain text
data/inbox/            Vendor replies as received;  data/extracted/  saved AI reads
evals/                 Answer key + scorer:  python -m evals.run_evals
tests/                 Normalization math vs answer key:  python -m tests.test_normalize
docs/DECISIONS.md      What was decided and what was left out
```
