"""Matching + normalization vs the answer key, using golden extractions.
Run: python -m tests.test_normalize"""
from __future__ import annotations

from pathlib import Path

import pandas as pd

from core.matching import match_vendor
from core.normalize import normalize
from core.rfq import Assumptions, load_rfq
from core.schema import VendorResponse

ROOT = Path(__file__).resolve().parent.parent
TOL = 0.01  # 1% — Gupta freight uses our box weight, not his slightly different size


def run(verbose: bool = True) -> dict:
    lines = load_rfq(); by = {l.code: l for l in lines}
    a = Assumptions()
    key = pd.read_excel(ROOT / "evals" / "answer_key.xlsx", sheet_name="Normalized Prices")
    key = key[~key["Item code"].astype(str).str.endswith("-ALT")]
    expected = {(r["Vendor"], r["Item code"]): r["Normalized ₹/pc (landed, ex-GST)"] for _, r in key.iterrows()}

    got = {}
    for f in sorted((ROOT / "tests" / "fixtures").glob("golden_*.json")):
        resp = VendorResponse.model_validate_json(f.read_text())
        matches, unmatched = match_vendor(resp, lines)
        for m in matches:
            item = resp.items[m.item_index]
            if item.is_alternate_spec:
                continue
            got[(resp.vendor_name, m.rfq_code)] = normalize(by[m.rfq_code], resp, item, m, a)

    ok = bad = 0; problems = []
    for k, exp in expected.items():
        n = got.get(k)
        if pd.isna(exp):
            if n is None:
                ok += 1
            else:
                bad += 1; problems.append(f"{k}: expected NOT QUOTED, got {n.price:.2f}")
            continue
        if n is None or n.price is None:
            bad += 1; problems.append(f"{k}: expected {exp:.2f}, got nothing"); continue
        if abs(n.price / exp - 1) <= TOL:
            ok += 1
        else:
            bad += 1; problems.append(f"{k}: expected {exp:.2f}, got {n.price:.2f}")
    if verbose:
        print(f"{ok} correct, {bad} wrong of {ok + bad}")
        for p in problems:
            print("  ", p)
    return {"ok": ok, "bad": bad, "problems": problems}


if __name__ == "__main__":
    run()
