"""Score REAL extractions (data/extracted/) against the answer key.

Three separate scores, because they fail for different reasons:
  1. Price accuracy  — extracted → matched → normalized ₹/pc vs ground truth
  2. Coverage        — quoted / not-quoted calls correct for every vendor-line
  3. Facts & flags   — the things a buyer must not miss (footnote discount,
                       expired cert, handwriting, alternates, terms)
Run: python -m evals.run_evals"""
from __future__ import annotations

from pathlib import Path

import pandas as pd

from core.extract import load_ai_matches, load_extracted
from core.matching import match_vendor
from core.normalize import normalize
from core.rfq import Assumptions, load_rfq

ROOT = Path(__file__).resolve().parent.parent
TOL = 0.01


def price_and_coverage(resps, lines, a):
    by = {l.code: l for l in lines}
    key = pd.read_excel(ROOT / "evals" / "answer_key.xlsx", sheet_name="Normalized Prices")
    key = key[~key["Item code"].astype(str).str.endswith("-ALT")]
    got = {}
    ai = load_ai_matches()
    for v, r in resps.items():
        ms, _ = match_vendor(r, lines, ai.get(v))
        for m in ms:
            it = r.items[m.item_index]
            if not it.is_alternate_spec:
                got[(v, m.rfq_code)] = normalize(by[m.rfq_code], r, it, m, a)
    price_ok = price_n = cov_ok = cov_n = 0
    misses = []
    for _, row in key.iterrows():
        k = (row["Vendor"], row["Item code"]); exp = row["Normalized ₹/pc (landed, ex-GST)"]
        if k[0] not in resps:
            continue
        n = got.get(k)
        cov_n += 1
        quoted_exp = not pd.isna(exp)
        quoted_got = n is not None and n.price is not None
        if quoted_exp == quoted_got:
            cov_ok += 1
        else:
            misses.append(f"coverage {k}: expected {'quoted' if quoted_exp else 'NOT QUOTED'}, got "
                          f"{'%.2f' % n.price if quoted_got else 'nothing'}")
        if quoted_exp and quoted_got:
            price_n += 1
            if abs(n.price / exp - 1) <= TOL:
                price_ok += 1
            else:
                misses.append(f"price {k}: expected {exp:.2f}, got {n.price:.2f}")
    return price_ok, price_n, cov_ok, cov_n, misses


def facts(resps):
    R = resps
    checks = []

    def chk(name, fn):
        try:
            checks.append((name, bool(fn())))
        except Exception:
            checks.append((name, False))

    chk("Kraftline: 2 alternates flagged", lambda: sum(i.is_alternate_spec for i in R["Kraftline"].items) == 2)
    chk("Kraftline: per-1000 unit", lambda: all(i.price_unit.value == "per_1000" for i in R["Kraftline"].items))
    chk("Kraftline: payment 45 days", lambda: R["Kraftline"].payment_days == 45)
    chk("Kraftline: delivered, ex-GST", lambda: R["Kraftline"].freight_treatment == "delivered" and R["Kraftline"].gst_treatment == "exclusive")
    chk("Balaji: GST inclusive", lambda: R["Shree Balaji"].gst_treatment == "inclusive")
    chk("Balaji: 3% footnote discount found", lambda: any(abs(d.percent - 3) < 0.01 for d in R["Shree Balaji"].discounts))
    chk("Balaji: discount condition captured", lambda: any(d.condition and "10" in d.condition for d in R["Shree Balaji"].discounts))
    chk("Balaji: cert valid until 2026-09-15", lambda: any(c.valid_until == "2026-09-15" for c in R["Shree Balaji"].certificates))
    chk("Balaji: 12 per-kg lines", lambda: sum(i.price_unit.value == "per_kg" for i in R["Shree Balaji"].items) == 12)
    chk("Pacific: USD", lambda: all(i.currency == "USD" for i in R["Pacific"].items))
    chk("Pacific: validity 15 days", lambda: R["Pacific"].validity_days == 15)
    chk("Pacific: certificate answer vague/not given", lambda: any(q.question_no == 1 and q.status in ("vague", "not_answered", "partial") for q in R["Pacific"].questionnaire))
    chk("Gupta: inch sizes on rate-card boxes", lambda: all(i.size_unit == "inch" for i in R["Gupta"].items if i.product_type == "box"))
    chk("Gupta: per-100 unit", lambda: all(i.price_unit.value == "per_100" for i in R["Gupta"].items))
    chk("Gupta: 4 handwritten values", lambda: sum(i.handwritten for i in R["Gupta"].items) == 4)
    chk("Gupta: corrected 14x10x7 3-ply = 895", lambda: any("14" in (i.size_as_written or "") and "7" in (i.size_as_written or "") and i.ply == 3 and i.price_value == 895 for i in R["Gupta"].items))
    chk("Gupta: plain-box + print adder", lambda: any(i.plain_box_rate for i in R["Gupta"].items) and any(a.unit == "per_100_per_colour" for a in R["Gupta"].adders))
    chk("Gupta: ex-works, GST extra", lambda: R["Gupta"].freight_treatment == "ex_works" and R["Gupta"].gst_treatment == "exclusive")
    chk("Om Sai: 'same as last year' captured", lambda: any(i.price_unit.value == "same_as_last_year" for i in R["Om Sai"].items))
    chk("Om Sai: freight extra", lambda: R["Om Sai"].freight_treatment == "extra")
    chk("Om Sai: questionnaire refers elsewhere", lambda: all(q.status == "refers_elsewhere" for q in R["Om Sai"].questionnaire))
    return checks


def run():
    resps = load_extracted(); lines = load_rfq(); a = Assumptions()
    p_ok, p_n, c_ok, c_n, misses = price_and_coverage(resps, lines, a)
    fx = facts(resps)
    print(f"Price accuracy : {p_ok}/{p_n}")
    print(f"Coverage       : {c_ok}/{c_n}")
    print(f"Facts & flags  : {sum(ok for _, ok in fx)}/{len(fx)}")
    for name, ok in fx:
        if not ok:
            print("   MISSED", name)
    for m in misses:
        print("  ", m)
    return {"price": (p_ok, p_n), "coverage": (c_ok, c_n), "facts": fx, "misses": misses}


if __name__ == "__main__":
    run()
