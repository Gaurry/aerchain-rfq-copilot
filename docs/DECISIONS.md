# Decision log — source for the one-page note

Running list of what we decided, why, and what we left out. Updated as we build.

## Framing
- **Core principle:** AI reads and interprets. Code calculates. Humans decide.
- **Where the effort went:** extraction, normalization and trust. RFQ drafting is deliberately thin, since any LLM can write an RFQ and it isn't where buyers lose days.
- **The better problem (candidate):** the hard part isn't reading documents, it's deciding whether two boxes are the same spec. "Is Gupta's 15×11×6 inch box your CB-309?" is where real money leaks, and no OCR solves it.

## Category and dataset
- **Corrugated packaging**, chosen because units are genuinely messy (per box, per kg, per 100, per 1,000, inches vs mm) and Indian packaging vendors really do send WhatsApp photos and one-line emails.
- **Buyer:** mid-size FMCG foods maker, Bawal plant. 30 lines, ~₹4.1 Cr annual spend (matches the brief's "₹4 crore on the line").
- **Box weight is computed, not invented:** liner GSM + flute GSM × 1.45 take-up, blank area, 4% trim. It lets code convert ₹/kg to ₹/box transparently.
- **Last year's prices are stored**, so "rest same as last year" is resolvable. Procurement isn't a one-off event.
- **Five vendors, one capability each:**
  - Kraftline (Excel, own codes, per 1,000, 27 of 30, value-engineered alternates)
  - Shree Balaji (PDF, GST-inclusive, 3% discount in a 6pt footnote, expired ISO certificate)
  - Pacific (Word, all prices in prose, USD, 15-day validity, vague questionnaire)
  - Gupta (angled phone photo, inches, per 100, plain-box rates, handwritten corrections, no questionnaire)
  - Om Sai (one-line email: per-kg rates, "rest same as last year", freight extra)
- **Planted story:** Balaji looks 17% pricier than last year but is cheapest on 19 lines once GST is removed and the footnote discount applied. Its expired certificate costs ~₹13 L/year (strict award ₹407.8 L vs ₹394.9 L with Balaji). The right move is "get the cert renewed," not "reject Balaji."

## Architecture
- **Streamlit + Python + Claude API + DuckDB**, hosted on Streamlit Community Cloud. One codebase. Vercel and Netlify were rejected because they don't run a long-lived Python server, and extraction calls take 20–60s.
- **No training or fine-tuning.** A general model plus a strict schema, domain context, a few examples and an eval set. Fine-tuning needs thousands of examples and is opaque; prompts change in minutes.
- **Fixed pipeline for extraction, not an autonomous agent.** Easier to debug, explain and audit. Agentic behaviour is reserved for the buyer Q&A, where questions are open-ended.
- **Prompts live as separate files**, so behaviour can change without code changes and every change can be re-scored against the answer key.

## Extraction and matching
- **Extract literally:** values, units and currency exactly as written, plus the source text as evidence. No conversion inside the model; otherwise reading errors and conversion errors blur together.
- **Matching order:** exact code → spec (ply + dimensions, inches converted) → category rules ("the 5-ply") → remaining rules ("rest"). Every rule-based expansion is flagged as an interpretation.
- **Near sizes are capped at 10% deviation.** Above that it's a different box. Between 5% and 10% it's matched but flagged red (e.g. Gupta's 20×14×13 vs CB-508, 6.4% off).
- **Product type must match.** A box can't match a partition just because sizes are close.
- **"Not quoted" is explicit**, never zero or blank, so a vendor's total can't look artificially low.

- **Items described only in words go to an AI matcher; rules then accept or reject.** Pacific wrote "the namkeen 150g shipper" with no code or size. The model proposes a match with a reason and confidence; code rejects anything that breaks ply, product type or one-to-one rules. The AI suggests, the rules decide.
- **Extraction model: Claude Sonnet 5.5.** Strong on documents and photos, far cheaper than Opus. Roughly 35–45s per vendor file; results are saved, with a re-run button, so the demo never waits on the API.
- **The model doesn't guess units.** Gupta's handwritten "Pad 16x12" has no unit, so it recorded "unknown". The matcher infers inches from the magnitude and flags it. Honest uncertainty beats a confident guess.

## Results from real extraction (first run)
- Prices 133/133 within 1% of the answer key; coverage 150/150 (every quoted / not-quoted call right); facts and flags 21/21 (footnote discount, expired cert date, 4 handwritten values, 2 alternates, USD, terms).
- First run scored 129/150 on coverage: the gap was description-only items, which led to the AI matching step. That's the eval loop working as intended.

## Review and comparison screens
- **Flags are grouped, not listed per line.** Balaji's conditional discount touches 30 lines but is one decision, so it's one review card. 106 flagged cells collapse into 14 decisions.
- **Three buyer actions:** approve, correct the value, or exclude the line. Decisions sit on top of the extracted data and never overwrite it; each one is logged to a corrections file that later becomes test cases.
- **The quality gate judges certificates by date, not by the vendor's "yes".** Balaji says yes and attaches a certificate that expired 15 days before the due date: conditional. Kraftline states a valid number but attaches nothing: cleared, with a note. Om Sai's "same as last year" is resolved to last year's answers on file: cleared, marked stale.
- **"Who can win a line?" is a toggle on the comparison:** cleared only, cleared + conditional, or all. Flipping it is how the buyer sees what quality costs (₹407.8 L → ₹394.9 L).
- **An unmarked price inherits the response's currency, with a review flag.** Gupta's handwritten pad price has no ₹ next to it; the rest of his card is INR.
- **Every price cell opens its working:** the ledger, the vendor's own words, match confidence and flags.

## Normalization (deterministic)
- **One comparable basis:** ₹ per piece, landed at Bawal, before GST.
- **Every step recorded in a ledger** the buyer can open from any cell (e.g. ₹49.02/kg incl. GST → ÷1.18 → −3% → × 0.566 kg = ₹22.81).
- **Blanket per-kg rates assumed to exclude printing** (last year's print adder applied, flagged). Line-by-line per-kg rates treated as all-in.
- **Conditional discounts** (Balaji's "PO above ₹10 L") applied by default but toggleable and always flagged.
- **All assumptions in one editable panel:** FX ₹88, freight ₹2/kg, GST 18%, review thresholds. Change one and everything recalculates, which proves the numbers aren't hardcoded.

## Trust
- **Confidence comes from evidence signals, not the model's opinion of itself:** source found, size deviation, price within ±40% of last year, handwritten, legibility.
- **A confident wrong answer is worse than an unnecessary flag.** Same trade-off as false negatives in AML screening. Start by over-flagging, then tune thresholds on real corrections.
- **Evals:** the answer key scores field accuracy, match accuracy and flag recall separately. The normalization engine scores 150/150 against it.
- **Test fixtures check the math only.** The app never loads them; every number shown comes from live AI extraction.

## Deliberately left out
- Real email, vendor portal, login, multiple users
- Rich RFQ editing (co-pilot drafts; editing is basic)
- PDF export (Excel only, PDF if time allows)
- Fine-tuning, multi-agent orchestration
- Negotiation rounds, reverse auctions, PO creation

## Production path (if asked)
- React frontend on Vercel/Netlify plus a separate Python extraction service.
- Identity federation on AWS/GCP instead of static API keys.
- Buyer corrections stored as examples; fine-tuning only once there are thousands, and only for cost or speed.
- A spec-equivalence library per category (what counts as "the same box"), built from buyer decisions over time.
