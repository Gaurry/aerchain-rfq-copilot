"""Supplier questionnaire gate. Deterministic rules over what extraction
recorded, so the quality head can see exactly why a vendor passed or not.

Statuses: cleared · cleared_stale (answers on file, not re-confirmed) ·
conditional (fixable, e.g. expired certificate) · failed · unknown."""
from __future__ import annotations

import re
from dataclasses import dataclass, field

import pandas as pd

from .rfq import QUESTIONNAIRE, Assumptions, load_vendor_history
from .schema import VendorResponse

MANDATORY = [q["no"] for q in QUESTIONNAIRE if q["type"] == "mandatory"]
LABEL = {"cleared": "Cleared", "cleared_stale": "Cleared (old answers)", "conditional": "Conditional",
         "failed": "Failed", "unknown": "Unknown"}


@dataclass
class Gate:
    vendor: str
    status: str
    reasons: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    @property
    def label(self) -> str:
        return LABEL[self.status]


def _max_days(text: str | None) -> int | None:
    nums = [int(n) for n in re.findall(r"\d+", text or "")]
    return max(nums) if nums else None


HISTORY_NAMES = {"Om Sai": "Om Sai Packaging"}


def assess(resp: VendorResponse, a: Assumptions, history: pd.DataFrame | None = None) -> Gate:
    v = resp.vendor_name
    qs = {q.question_no: q for q in resp.questionnaire}
    statuses = {q.status for q in qs.values()}

    if not qs or statuses == {"not_answered"}:
        return Gate(v, "unknown", ["Questionnaire not returned"])

    if statuses == {"refers_elsewhere"}:
        history = load_vendor_history() if history is None else history
        hist = history[history["Vendor"] == HISTORY_NAMES.get(v, v)]
        if hist.empty:
            return Gate(v, "unknown", ["Refers to an earlier submission that isn't on file"])
        dated = hist[hist["Item"].astype(str).str.startswith("Q")]
        when = dated["Date on file"].iloc[0] if not dated.empty else "an earlier date"
        iso = hist[hist["Item"].astype(str).str.startswith("Q1")]["Value"]
        g = Gate(v, "cleared_stale", [f"Answers taken from the submission on file ({when}); not re-confirmed"])
        if not iso.empty:
            g.notes.append(f"ISO on file: {iso.iloc[0]}")
        return g

    g = Gate(v, "cleared")
    fails, conds = [], []

    # Q1 certificate: judged on the date, not on the vendor's "yes"
    certs = [c for c in resp.certificates if "9001" in c.standard]
    q1 = qs.get(1)
    if certs:
        c = max(certs, key=lambda c: c.valid_until or "")
        if c.valid_until and c.valid_until < a.rfq_due_date:
            conds.append(f"ISO 9001 certificate expired {c.valid_until}, before the RFQ due date")
        elif c.source == "stated_in_response":
            g.notes.append(f"ISO 9001 valid to {c.valid_until} (stated; copy not attached)")
    elif q1 is None or q1.status in ("no", "vague", "not_answered"):
        fails.append("No verifiable ISO 9001 certificate")
    else:
        conds.append("ISO 9001 claimed but no certificate or date given")

    for no in (2, 5):
        q = qs.get(no)
        if q is None or q.status in ("no", "vague", "not_answered"):
            fails.append(f"Q{no} not met: {q.answer_text if q else 'not answered'}"[:120])

    days = _max_days((qs[4].answer_text if 4 in qs else None) or resp.lead_time_text)
    if days is None:
        fails.append("Lead time not given")
    elif days > 10:
        fails.append(f"Lead time up to {days} days (limit 10)")

    if resp.payment_days and resp.payment_days != 60:
        g.notes.append(f"Payment {resp.payment_days} days instead of 60")
    if resp.validity_days and resp.validity_days < 90:
        g.notes.append(f"Price validity only {resp.validity_days} days")

    if fails:
        g.status, g.reasons = "failed", fails + conds
    elif conds:
        g.status, g.reasons = "conditional", conds
    return g
